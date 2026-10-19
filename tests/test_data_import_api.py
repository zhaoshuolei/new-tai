"""Data import upload, status, error, and template API tests."""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from openpyxl import load_workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import data_imports as data_import_api
from app.core.audit_meta import ActorSnapshot, get_audit_route_meta
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.data_management import (
    DataImportError,
    DataImportJob,
    DataImportSource,
    DataImportStatus,
)
from app.models.user import User
from app.schemas.data_import import parse_data_import_json_request
from app.schemas.data_table import DataTableCreate
from app.services import audit as audit_service
from app.services import data_table as data_table_service
from app.services.data_import_parser import build_excel_template
from tests.conftest import FakeRedis
from tests.customer_helpers import (  # noqa: F401
    TEST_DATABASE_URL,
    make_context,
    make_user,
    session,
)

_ALL_DATA_PERMISSIONS = {
    "data:read",
    "data:create",
    "data:update",
    "data:delete",
    "data:import",
}
_IMPORT_AUDIT_ACTION = "data_import:request"
_CLEANED_AUDIT_ACTIONS = (_IMPORT_AUDIT_ACTION, "data_table:create")
_PRIVATE_JOB_FIELDS = {
    "storage_path",
    "content_sha256",
    "lease_owner",
    "lease_expires_at",
    "heartbeat_at",
}
_HARD_MAX_IMPORT_BYTES = 50 * 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


@pytest.fixture(autouse=True)
def configure_import_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / "data-imports"
    monkeypatch.setattr(settings, "data_import_root", str(root))
    monkeypatch.setattr(settings, "data_import_max_bytes", 50 * 1024 * 1024)
    return root


@pytest_asyncio.fixture(autouse=True)
async def import_audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(
                delete(AuditLog).where(AuditLog.action.in_(_CLEANED_AUDIT_ACTIONS))
            )
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


def _context(
    user: User,
    *,
    permissions: set[str] | None = None,
) -> AuthContext:
    selected = permissions if permissions is not None else _ALL_DATA_PERMISSIONS
    return make_context(user, permission_codes=selected)


def _table_payload(name: str | None = None) -> DataTableCreate:
    return DataTableCreate.model_validate(
        {
            "name": name or f"import-api-{uuid4().hex[:10]}",
            "description": "Import API test table",
            "fields": [
                {
                    "label": "Business Date",
                    "field_type": "DATE",
                    "system_key": "BUSINESS_DATE",
                    "sort_order": 0,
                },
                {
                    "label": "Upstream",
                    "field_type": "CUSTOMER",
                    "system_key": "UPSTREAM_CUSTOMER",
                    "sort_order": 1,
                },
                {
                    "label": "Downstream",
                    "field_type": "CUSTOMER",
                    "system_key": "DOWNSTREAM_CUSTOMER",
                    "sort_order": 2,
                },
                {
                    "label": "Revenue",
                    "field_type": "DECIMAL",
                    "system_key": "REVENUE",
                    "sort_order": 3,
                },
                {
                    "label": "Cost",
                    "field_type": "DECIMAL",
                    "system_key": "COST",
                    "sort_order": 4,
                },
                {
                    "label": "Channel",
                    "field_type": "TEXT",
                    "system_key": "CUSTOM",
                    "sort_order": 5,
                },
            ],
        }
    )


async def _create_table(
    session: AsyncSession,
    user: User,
    *,
    name: str | None = None,
) -> Any:
    return await data_table_service.create_table(
        session,
        _table_payload(name),
        context=_context(user),
    )


@asynccontextmanager
async def _api_client(
    session: AsyncSession,
    context: AuthContext | None,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_redis] = FakeRedis
    if context is not None:

        async def override_context(request: Request) -> AuthContext:
            request.state.user_id = context.user_id
            request.state.actor = ActorSnapshot(
                user_id=context.user_id,
                username=context.username,
                nickname=context.nickname,
            )
            return context

        application.dependency_overrides[get_current_context] = override_context

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


def _job(
    *,
    table_id: int,
    user_id: int,
    status: DataImportStatus = DataImportStatus.FAILED,
    source: DataImportSource = DataImportSource.API,
    original_filename: str | None = "source.json",
) -> DataImportJob:
    suffix = ".xlsx" if source == DataImportSource.EXCEL else ".json"
    return DataImportJob(
        table_id=table_id,
        source=source,
        status=status,
        original_filename=original_filename,
        storage_path=f"{uuid4()}{suffix}",
        content_sha256="a" * 64,
        size_bytes=10,
        requested_by=user_id,
    )


async def _jobs(session: AsyncSession, table_id: int) -> list[DataImportJob]:
    return list(
        (
            await session.scalars(
                select(DataImportJob)
                .where(DataImportJob.table_id == table_id)
                .order_by(DataImportJob.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.action == _IMPORT_AUDIT_ACTION)
                    .order_by(AuditLog.id)
                )
            ).all()
        )


def _json_body(revenue: str = "10.25", cost: str = "4.75") -> bytes:
    return (
        "{\n"
        '  "rows": [{\n'
        '    "business_date": "2026-10-18",\n'
        '    "upstream_customer_id": 101,\n'
        '    "downstream_customer_id": 202,\n'
        f'    "revenue": {revenue},\n'
        f'    "cost": {cost},\n'
        '    "custom_values": {}\n'
        "  }]\n"
        "}\n"
    ).encode()


async def _chunked_content(
    content: bytes,
    *,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    for offset in range(0, len(content), chunk_size):
        yield content[offset : offset + chunk_size]


async def _assert_no_import_residue(
    session: AsyncSession,
    audit_factory: async_sessionmaker[AsyncSession],
    storage_root: Path,
    table_id: int,
) -> None:
    assert await _jobs(session, table_id) == []
    assert await _audit_rows(audit_factory) == []
    assert not storage_root.exists() or list(storage_root.iterdir()) == []


async def test_excel_import_returns_202_and_persists_exact_source(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    raw_xlsx = build_excel_template(definition)

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            files={
                "file": (
                    "../../day.xlsx",
                    raw_xlsx,
                    "application/octet-stream",
                )
            },
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == DataImportStatus.QUEUED
    assert body["data"]["source"] == DataImportSource.EXCEL
    assert body["data"]["original_filename"] == "day.xlsx"
    assert _PRIVATE_JOB_FIELDS.isdisjoint(body["data"])

    jobs = await _jobs(session, definition.id)
    assert len(jobs) == 1
    job = jobs[0]
    assert (configure_import_storage / job.storage_path).read_bytes() == raw_xlsx
    assert job.content_sha256 == hashlib.sha256(raw_xlsx).hexdigest()

    audits = await _audit_rows(import_audit_session_factory)
    assert len(audits) == 1
    assert audits[0].result is AuditResult.SUCCESS
    assert audits[0].resource_type == "custom_data_import"
    assert audits[0].resource_id == job.id


async def test_excel_import_rejects_missing_or_non_xlsx_file(
    session: AsyncSession,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)

    async with _api_client(session, _context(caller)) as client:
        missing = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            data={"file": "not-a-file"},
        )
        wrong_suffix = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            files={"file": ("day.csv", b"a,b\n1,2\n", "text/csv")},
        )

    assert missing.status_code == 422
    assert missing.json()["code"] == ErrorCode.INVALID_PARAMETER
    assert wrong_suffix.status_code == 422
    assert wrong_suffix.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    assert await _jobs(session, definition.id) == []
    assert (
        not configure_import_storage.exists()
        or list(configure_import_storage.iterdir()) == []
    )


async def test_excel_import_enforces_byte_limit_before_enqueue(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    monkeypatch.setattr(settings, "data_import_max_bytes", 8)

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            files={
                "file": (
                    "oversized.xlsx",
                    b"123456789",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    assert await _jobs(session, definition.id) == []
    assert (
        not configure_import_storage.exists()
        or list(configure_import_storage.iterdir()) == []
    )


async def test_enqueue_failure_removes_newly_saved_source(
    session: AsyncSession,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    definition_id = definition.id
    existing = _job(
        table_id=definition_id,
        user_id=caller.id,
        status=DataImportStatus.QUEUED,
    )
    session.add(existing)
    await session.commit()
    existing_id = existing.id
    existing_storage_path = existing.storage_path
    existing_path = configure_import_storage / existing_storage_path
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_bytes(b"existing")

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition_id}/imports/json",
            content=_json_body(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_IN_PROGRESS
    assert [job.id for job in await _jobs(session, definition_id)] == [existing_id]
    assert sorted(path.name for path in configure_import_storage.iterdir()) == [
        existing_storage_path
    ]


async def test_enqueue_rollback_delete_failure_logs_only_generic_warning(
    session: AsyncSession,
    configure_import_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    definition_id = definition.id
    existing = _job(
        table_id=definition_id,
        user_id=caller.id,
        status=DataImportStatus.QUEUED,
    )
    session.add(existing)
    await session.commit()

    private_name = "private-customer-source.xlsx"
    source_marker = b"private-source-bytes"
    saved_sources = []
    real_save = data_import_api.save_import_bytes

    def record_saved_source(*args: Any, **kwargs: Any) -> Any:
        stored = real_save(*args, **kwargs)
        saved_sources.append(stored)
        return stored

    def fail_delete(storage_path: str) -> None:
        raise PermissionError(
            f"cannot delete {configure_import_storage / storage_path} "
            f"for {private_name} containing {source_marker!r}"
        )

    monkeypatch.setattr(data_import_api, "save_import_bytes", record_saved_source)
    monkeypatch.setattr(data_import_api, "delete_import_source", fail_delete)
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="app.api.data_imports")

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition_id}/imports/excel",
            files={
                "file": (
                    private_name,
                    source_marker,
                    "application/octet-stream",
                )
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_IN_PROGRESS
    assert [job.id for job in await _jobs(session, definition_id)] == [existing.id]
    assert len(saved_sources) == 1
    stored = saved_sources[0]
    assert (configure_import_storage / stored.storage_path).is_file()
    assert "Failed to remove an unsaved data import source" in caplog.text
    assert str(configure_import_storage) not in caplog.text
    assert stored.storage_path not in caplog.text
    assert private_name not in caplog.text
    assert source_marker.decode() not in caplog.text


async def test_json_import_persists_exact_raw_bytes_and_exact_decimal(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    raw_json = _json_body(
        revenue="12345678901234567890.12345678",
        cost="1.23000000",
    )

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=raw_json,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 202, response.text
    assert response.json()["data"]["source"] == DataImportSource.API
    assert _PRIVATE_JOB_FIELDS.isdisjoint(response.json()["data"])

    jobs = await _jobs(session, definition.id)
    assert len(jobs) == 1
    job = jobs[0]
    persisted = (configure_import_storage / job.storage_path).read_bytes()
    assert persisted == raw_json
    parsed = parse_data_import_json_request(persisted)
    assert parsed.rows[0].revenue == Decimal("12345678901234567890.12345678")
    assert parsed.rows[0].cost == Decimal("1.23000000")

    audits = await _audit_rows(import_audit_session_factory)
    assert len(audits) == 1
    assert audits[0].result is AuditResult.SUCCESS
    assert audits[0].resource_id == job.id


async def test_json_import_rejects_malformed_body_before_storage(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=b'{"rows": [',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    assert response.json()["message"] == "Import JSON is invalid"
    await _assert_no_import_residue(
        session,
        import_audit_session_factory,
        configure_import_storage,
        definition.id,
    )


async def test_json_import_rejects_more_than_100000_rows_before_storage(
    session: AsyncSession,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    row = {
        "business_date": "2026-10-18",
        "upstream_customer_id": 1,
        "downstream_customer_id": 2,
        "revenue": "1.00",
        "cost": "0.50",
    }
    raw_json = json.dumps(
        {"rows": [row] * 100_001},
        separators=(",", ":"),
    ).encode()

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=raw_json,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    assert await _jobs(session, definition.id) == []
    assert (
        not configure_import_storage.exists()
        or list(configure_import_storage.iterdir()) == []
    )


async def test_json_import_enforces_byte_limit_before_storage(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    monkeypatch.setattr(settings, "data_import_max_bytes", 32)
    raw_json = _json_body()

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=raw_json,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    assert await _jobs(session, definition.id) == []
    assert (
        not configure_import_storage.exists()
        or list(configure_import_storage.iterdir()) == []
    )


async def test_json_chunked_body_without_content_length_is_rejected(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    monkeypatch.setattr(settings, "data_import_max_bytes", 128)
    raw_json = _json_body()

    async def unbounded_body_read(request: Request) -> bytes:
        await request.body()
        raise AssertionError("receive guard did not reject the body")

    monkeypatch.setattr(data_import_api, "_read_bounded_body", unbounded_body_read)

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=_chunked_content(raw_json, chunk_size=37),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    await _assert_no_import_residue(
        session,
        import_audit_session_factory,
        configure_import_storage,
        definition.id,
    )


async def test_json_lying_small_content_length_is_rejected_by_actual_bytes(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    monkeypatch.setattr(settings, "data_import_max_bytes", 128)
    raw_json = _json_body()

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=raw_json,
            headers={
                "content-type": "application/json",
                "content-length": "64",
            },
        )

    assert len(raw_json) > 128
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    await _assert_no_import_residue(
        session,
        import_audit_session_factory,
        configure_import_storage,
        definition.id,
    )


async def test_json_exact_50_mib_body_is_accepted(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    prefix = b'{"rows":[]}'
    raw_json = prefix + b" " * (_HARD_MAX_IMPORT_BYTES - len(prefix))
    assert len(raw_json) == _HARD_MAX_IMPORT_BYTES

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=raw_json,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 202, response.text
    jobs = await _jobs(session, definition.id)
    assert len(jobs) == 1
    assert jobs[0].size_bytes == _HARD_MAX_IMPORT_BYTES
    assert (
        configure_import_storage / jobs[0].storage_path
    ).stat().st_size == _HARD_MAX_IMPORT_BYTES
    audits = await _audit_rows(import_audit_session_factory)
    assert len(audits) == 1
    assert audits[0].result is AuditResult.SUCCESS


async def test_chunked_multipart_is_rejected_at_receive_boundary(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    monkeypatch.setattr(settings, "data_import_max_bytes", 4096)
    boundary = "chunked-import-boundary"
    file_content = b"x" * (4096 + _MULTIPART_OVERHEAD_BYTES + 1)
    multipart = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="large.xlsx"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + file_content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    endpoint_read_called = False

    async def unexpected_upload_read(file: Any) -> bytes:
        nonlocal endpoint_read_called
        endpoint_read_called = True
        raise AssertionError("multipart parser reached the endpoint")

    monkeypatch.setattr(
        data_import_api,
        "_read_bounded_upload",
        unexpected_upload_read,
    )

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            content=_chunked_content(multipart, chunk_size=8192),
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_INVALID
    assert endpoint_read_called is False
    await _assert_no_import_residue(
        session,
        import_audit_session_factory,
        configure_import_storage,
        definition.id,
    )


async def test_excel_exact_50_mib_file_is_accepted_with_multipart_margin(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    raw_xlsx = b"x" * _HARD_MAX_IMPORT_BYTES
    assert len(raw_xlsx) == _HARD_MAX_IMPORT_BYTES

    async with _api_client(session, _context(caller)) as client:
        response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            files={
                "file": (
                    "exact-boundary.xlsx",
                    raw_xlsx,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 202, response.text
    jobs = await _jobs(session, definition.id)
    assert len(jobs) == 1
    assert jobs[0].size_bytes == _HARD_MAX_IMPORT_BYTES
    assert (
        configure_import_storage / jobs[0].storage_path
    ).stat().st_size == _HARD_MAX_IMPORT_BYTES
    audits = await _audit_rows(import_audit_session_factory)
    assert len(audits) == 1
    assert audits[0].result is AuditResult.SUCCESS


async def test_import_post_permission_denials_write_one_audit_each(
    session: AsyncSession,
    import_audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    denied_context = _context(caller, permissions={"data:read"})

    async with _api_client(session, denied_context) as client:
        excel = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/excel",
            files={"file": ("day.xlsx", b"xlsx", "application/octet-stream")},
        )
        json_response = await client.post(
            f"/api/v1/data-tables/{definition.id}/imports/json",
            content=_json_body(),
            headers={"content-type": "application/json"},
        )

    assert excel.status_code == 403
    assert json_response.status_code == 403
    assert await _jobs(session, definition.id) == []

    audits = await _audit_rows(import_audit_session_factory)
    assert len(audits) == 2
    assert {row.result for row in audits} == {AuditResult.DENIED}
    assert {row.resource_type for row in audits} == {"custom_data_import"}
    assert {row.resource_id for row in audits} == {definition.id}
    assert {row.reason for row in audits} == {"FORBIDDEN"}


async def test_import_list_status_and_errors_are_paginated_and_safe(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    other_definition = await _create_table(session, caller)
    first = _job(table_id=definition.id, user_id=caller.id)
    second = _job(table_id=definition.id, user_id=caller.id)
    third = _job(table_id=definition.id, user_id=caller.id)
    foreign = _job(table_id=other_definition.id, user_id=caller.id)
    session.add_all([first, second, third, foreign])
    await session.flush()
    for index in range(3):
        session.add(
            DataImportError(
                import_id=first.id,
                row_number=index + 2,
                error_code="INVALID_ROW",
                message=f"Row {index + 2} is invalid",
            )
        )
    await session.flush()

    async with _api_client(session, _context(caller)) as client:
        listing = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports",
            params={"page": 1, "page_size": 2},
        )
        second_page = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports",
            params={"page": 2, "page_size": 2},
        )
        detail = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{first.id}"
        )
        errors = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{first.id}/errors",
            params={"page": 2, "page_size": 2},
        )
        foreign_lookup = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{foreign.id}"
        )
        missing_table = await client.get(
            "/api/v1/data-tables/0/imports",
            params={"page": 1, "page_size": 20},
        )
        very_large_page = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports",
            params={"page": 10**30, "page_size": 100},
        )
        very_large_error_page = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{first.id}/errors",
            params={"page": 10**30, "page_size": 100},
        )

    assert listing.status_code == 200, listing.text
    page = listing.json()["data"]
    assert page["total"] == 3
    assert page["page"] == 1
    assert page["page_size"] == 2
    assert len(page["items"]) == 2
    assert all(_PRIVATE_JOB_FIELDS.isdisjoint(item) for item in page["items"])

    assert second_page.status_code == 200
    assert second_page.json()["data"]["total"] == 3
    assert len(second_page.json()["data"]["items"]) == 1

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["id"] == first.id
    assert _PRIVATE_JOB_FIELDS.isdisjoint(detail.json()["data"])

    assert errors.status_code == 200, errors.text
    error_page = errors.json()["data"]
    assert error_page["total"] == 3
    assert error_page["page"] == 2
    assert error_page["page_size"] == 2
    assert [item["row_number"] for item in error_page["items"]] == [4]

    assert foreign_lookup.status_code == 404
    assert foreign_lookup.json()["code"] == ErrorCode.DATA_IMPORT_NOT_FOUND
    assert missing_table.status_code == 404
    assert missing_table.json()["code"] == ErrorCode.DATA_TABLE_NOT_FOUND

    assert very_large_page.status_code == 200
    assert very_large_page.json()["data"]["items"] == []
    assert very_large_page.json()["data"]["total"] == 3
    assert very_large_error_page.status_code == 200
    assert very_large_error_page.json()["data"]["items"] == []
    assert very_large_error_page.json()["data"]["total"] == 3


async def test_import_read_routes_require_import_permission(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    job = _job(table_id=definition.id, user_id=caller.id)
    session.add(job)
    await session.flush()
    denied_context = _context(caller, permissions={"data:read"})

    async with _api_client(session, denied_context) as client:
        listing = await client.get(f"/api/v1/data-tables/{definition.id}/imports")
        detail = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{job.id}"
        )

    assert listing.status_code == 403
    assert detail.status_code == 403


async def test_template_returns_configured_headers_and_is_offloaded(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    template_context = _context(caller, permissions={"data:read"})
    seen_threads: list[threading.Thread] = []
    real_builder = data_import_api.build_excel_template

    def recording_builder(resolved: Any) -> bytes:
        seen_threads.append(threading.current_thread())
        return real_builder(resolved)

    monkeypatch.setattr(data_import_api, "build_excel_template", recording_builder)

    async with _api_client(session, template_context) as client:
        response = await client.get(
            f"/api/v1/data-tables/{definition.id}/template.xlsx"
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="template.xlsx"'
    )
    assert response.headers["cache-control"] == "no-store"
    workbook = load_workbook(BytesIO(response.content), read_only=True)
    try:
        labels = [cell.value for cell in workbook.worksheets[0][1]]
    finally:
        workbook.close()
    assert labels == list(definition.labels)
    assert len(seen_threads) == 1
    assert seen_threads[0] is not threading.main_thread()


async def test_template_requires_data_read_permission(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    denied_context = _context(caller, permissions={"data:import"})

    async with _api_client(session, denied_context) as client:
        response = await client.get(
            f"/api/v1/data-tables/{definition.id}/template.xlsx"
        )

    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.FORBIDDEN


def test_import_routes_register_audit_metadata_and_openapi_tag() -> None:
    expected = {
        ("POST", "/data-tables/{table_id}/imports/excel"),
        ("POST", "/data-tables/{table_id}/imports/json"),
    }
    for method, path in expected:
        meta = get_audit_route_meta(method, path)
        assert meta is not None
        assert meta.action == "data_import:request"
        assert meta.resource_type == "custom_data_import"
        assert meta.resource_id_param == "table_id"

    document = create_app().openapi()
    data_tag = next(tag for tag in document["tags"] if tag["name"] == "数据管理")
    assert "导入" in data_tag["description"]
    assert "模板" in data_tag["description"]
    assert "当前数据" in data_tag["description"]
    expected_paths = {
        "/api/v1/data-tables/{table_id}/imports/excel",
        "/api/v1/data-tables/{table_id}/imports/json",
        "/api/v1/data-tables/{table_id}/imports",
        "/api/v1/data-tables/{table_id}/imports/{import_id}",
        "/api/v1/data-tables/{table_id}/imports/{import_id}/errors",
        "/api/v1/data-tables/{table_id}/imports/{import_id}/source",
        "/api/v1/data-tables/{table_id}/template.xlsx",
    }
    for path in expected_paths:
        assert path in document["paths"]
        for operation in document["paths"][path].values():
            assert operation["tags"] == ["数据管理"]
