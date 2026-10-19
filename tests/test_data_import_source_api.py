"""Authenticated import source download and DRAFT cleanup tests."""

# ruff: noqa: F811

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import data_imports as data_import_api
from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.data_management import (
    DataImportJob,
    DataImportSource,
    DataImportStatus,
    DataTableDefinition,
    DataTableStatus,
)
from app.models.user import User
from app.schemas.data_table import DataTableCreate
from app.services import audit as audit_service
from app.services import data_import as data_import_service
from app.services import data_import_storage
from app.services import data_table as data_table_service
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
_CLEANED_AUDIT_ACTIONS = (
    "data_import:request",
    "data_table:create",
    "data_table:delete",
)


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
            "name": name or f"import-source-{uuid4().hex[:10]}",
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


async def _enqueue_source(
    session: AsyncSession,
    *,
    table_id: int,
    user: User,
    content: bytes,
    suffix: str,
    original_name: str,
) -> tuple[DataImportJob, data_import_storage.StoredImportSource]:
    stored = data_import_storage.save_import_bytes(
        content,
        suffix=suffix,  # type: ignore[arg-type]
        original_name=original_name,
    )
    job = await data_import_service.enqueue_import(
        session,
        table_id,
        source=(DataImportSource.EXCEL if suffix == ".xlsx" else DataImportSource.API),
        stored=stored,
        context=_context(user),
    )
    return job, stored


async def test_source_download_streams_exact_authenticated_bytes_without_private_path(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    raw_source = b'{"rows":[]}\n'
    job, stored = await _enqueue_source(
        session,
        table_id=definition.id,
        user=caller,
        content=raw_source,
        suffix=".json",
        original_name="../../source.json",
    )
    seen_threads: list[threading.Thread] = []
    real_reader = data_import_api.read_import_source

    def recording_reader(storage_path: str) -> Any:
        seen_threads.append(threading.current_thread())
        return real_reader(storage_path)

    monkeypatch.setattr(data_import_api, "read_import_source", recording_reader)

    async with _api_client(session, _context(caller)) as client:
        response = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{job.id}/source"
        )

    assert response.status_code == 200, response.text
    assert response.content == raw_source
    assert response.headers["content-type"] == "application/json"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="source.json"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-length"] == str(len(raw_source))
    assert stored.storage_path not in response.headers.values()
    assert str(settings.data_import_root) not in response.headers.values()
    assert len(seen_threads) == 1
    assert seen_threads[0] is not threading.main_thread()


async def test_source_download_requires_import_permission_and_authentication(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    job, _ = await _enqueue_source(
        session,
        table_id=definition.id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="source.json",
    )
    denied_context = _context(caller, permissions={"data:read"})

    async with _api_client(session, denied_context) as client:
        denied = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{job.id}/source"
        )
    async with _api_client(session, None) as client:
        unauthenticated = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{job.id}/source"
        )

    assert denied.status_code == 403
    assert denied.json()["code"] == ErrorCode.FORBIDDEN
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == ErrorCode.UNAUTHENTICATED


async def test_source_download_returns_9005_for_missing_foreign_or_absent_source(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    other_definition = await _create_table(session, caller)
    job, stored = await _enqueue_source(
        session,
        table_id=definition.id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="source.json",
    )
    foreign, _ = await _enqueue_source(
        session,
        table_id=other_definition.id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="foreign.json",
    )
    (Path(settings.data_import_root) / stored.storage_path).unlink()

    async with _api_client(session, _context(caller)) as client:
        missing_source = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{job.id}/source"
        )
        foreign_source = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/{foreign.id}/source"
        )
        absent_job = await client.get(
            f"/api/v1/data-tables/{definition.id}/imports/0/source"
        )

    for response in (missing_source, foreign_source, absent_job):
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.DATA_IMPORT_NOT_FOUND


async def test_source_download_hides_tampered_storage_path_as_not_found(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    definition_id = definition.id
    job, _ = await _enqueue_source(
        session,
        table_id=definition_id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="source.json",
    )
    job_id = job.id
    job.storage_path = "../outside.json"
    await session.commit()

    async with _api_client(session, _context(caller)) as client:
        response = await client.get(
            f"/api/v1/data-tables/{definition_id}/imports/{job_id}/source"
        )

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.DATA_IMPORT_NOT_FOUND


async def test_draft_delete_removes_private_sources_after_database_cascade(
    session: AsyncSession,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    job, stored = await _enqueue_source(
        session,
        table_id=definition.id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="source.json",
    )
    source_path = configure_import_storage / stored.storage_path
    assert source_path.is_file()
    job.status = DataImportStatus.FAILED
    await session.flush()

    await data_table_service.delete_table(
        session,
        definition.id,
        context=_context(caller),
    )

    assert await session.get(DataTableDefinition, definition.id) is None
    assert await session.get(DataImportJob, job.id) is None
    assert not source_path.exists()


async def test_locked_table_delete_keeps_private_sources(
    session: AsyncSession,
    configure_import_storage: Path,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    job, stored = await _enqueue_source(
        session,
        table_id=definition.id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="source.json",
    )
    source_path = configure_import_storage / stored.storage_path
    job.status = DataImportStatus.FAILED
    definition.table.status = DataTableStatus.LOCKED
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await data_table_service.delete_table(
            session,
            definition.id,
            context=_context(caller),
        )

    assert error.value.code == ErrorCode.DATA_TABLE_LOCKED
    assert source_path.is_file()
    assert await session.get(DataTableDefinition, definition.id) is not None


async def test_source_cleanup_failure_does_not_rollback_delete_or_log_private_values(
    session: AsyncSession,
    configure_import_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    job, stored = await _enqueue_source(
        session,
        table_id=definition.id,
        user=caller,
        content=b'{"rows":[]}',
        suffix=".json",
        original_name="private-customer-source.json",
    )
    source_path = configure_import_storage / stored.storage_path
    job.status = DataImportStatus.FAILED
    await session.flush()

    def fail_cleanup(storage_path: str) -> None:
        raise PermissionError(
            f"cannot remove {configure_import_storage / storage_path} "
            f"for {stored.original_name}"
        )

    monkeypatch.setattr(
        data_import_storage,
        "delete_import_source",
        fail_cleanup,
    )
    caplog.set_level(logging.WARNING)

    await data_table_service.delete_table(
        session,
        definition.id,
        context=_context(caller),
    )

    assert await session.get(DataTableDefinition, definition.id) is None
    assert await session.get(DataImportJob, job.id) is None
    assert source_path.is_file()
    assert str(configure_import_storage) not in caplog.text
    assert stored.storage_path not in caplog.text
    assert stored.original_name not in caplog.text


async def test_source_cleanup_queries_only_under_table_lifecycle_lock(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = await make_user(session)
    definition = await _create_table(session, caller)
    order: list[str] = []
    real_require_writable = data_table_service._require_writable_table
    real_delete_table = data_table_service.data_table_repository.delete_table

    async def record_require(*args: Any, **kwargs: Any) -> Any:
        order.append("lock")
        return await real_require_writable(*args, **kwargs)

    async def record_delete(*args: Any, **kwargs: Any) -> Any:
        order.append("db-delete")
        return await real_delete_table(*args, **kwargs)

    monkeypatch.setattr(
        data_table_service,
        "_require_writable_table",
        record_require,
    )
    monkeypatch.setattr(
        data_table_service.data_table_repository,
        "delete_table",
        record_delete,
    )

    await data_table_service.delete_table(
        session,
        definition.id,
        context=_context(caller),
    )

    assert order == ["lock", "db-delete"]
