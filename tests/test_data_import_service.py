"""Import enqueue and durable worker orchestration tests."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog, AuditResult
from app.models.customer import Customer
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportError,
    DataImportJob,
    DataImportSource,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
    DataTableStatus,
)
from app.models.role import DataScope
from app.models.user import User, UserStatus
from app.repositories import data_import as data_import_repository
from app.repositories.data_table import load_resolved_definition
from app.services import audit as audit_service
from app.services import data_import as data_import_service
from app.services import data_import_parser
from app.services.data_import_parser import build_excel_template
from app.services.data_import_storage import StoredImportSource, save_import_bytes
from tests.customer_helpers import TEST_DATABASE_URL, make_customer_row, make_user

DATE_ONE = date(2026, 10, 18)
DATE_TWO = date(2026, 10, 19)
DATE_UNCHANGED = date(2026, 10, 20)
IMPORT_AUDIT_ACTIONS = (
    "data_import:request",
    "data_import:complete",
    "data_import:fail",
)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


class _CommittedState:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.table_ids: list[int] = []
        self.user_ids: list[int] = []

    def track_table(self, table: DataTableDefinition) -> None:
        self.table_ids.append(table.id)

    def track_user(self, user: User) -> None:
        self.user_ids.append(user.id)


async def _cleanup(state: _CommittedState) -> None:
    async with state.factory() as session:
        await session.execute(
            sa.delete(AuditLog).where(AuditLog.action.in_(IMPORT_AUDIT_ACTIONS))
        )
        if state.table_ids:
            record_ids = sa.select(DataRecord.id).where(
                DataRecord.table_id.in_(state.table_ids)
            )
            await session.execute(
                sa.delete(DataRecordValue).where(
                    DataRecordValue.record_id.in_(record_ids)
                )
            )
            await session.execute(
                sa.delete(DataRecord).where(DataRecord.table_id.in_(state.table_ids))
            )
            await session.execute(
                DataCurrentSnapshot.delete().where(
                    DataCurrentSnapshot.c.table_id.in_(state.table_ids)
                )
            )
            await session.execute(
                sa.delete(DataSnapshot).where(
                    DataSnapshot.table_id.in_(state.table_ids)
                )
            )
            import_ids = sa.select(DataImportJob.id).where(
                DataImportJob.table_id.in_(state.table_ids)
            )
            await session.execute(
                sa.delete(DataImportError).where(
                    DataImportError.import_id.in_(import_ids)
                )
            )
            await session.execute(
                sa.delete(DataImportJob).where(
                    DataImportJob.table_id.in_(state.table_ids)
                )
            )
            field_ids = sa.select(DataFieldDefinition.id).where(
                DataFieldDefinition.table_id.in_(state.table_ids)
            )
            await session.execute(
                sa.delete(DataEnumOption).where(DataEnumOption.field_id.in_(field_ids))
            )
            await session.execute(
                sa.delete(DataFieldDefinition).where(
                    DataFieldDefinition.table_id.in_(state.table_ids)
                )
            )
            await session.execute(
                sa.delete(DataTableDefinition).where(
                    DataTableDefinition.id.in_(state.table_ids)
                )
            )
        if state.user_ids:
            await session.execute(
                sa.delete(Customer).where(Customer.owner_user_id.in_(state.user_ids))
            )
            await session.execute(sa.delete(User).where(User.id.in_(state.user_ids)))
        await session.commit()


@pytest_asyncio.fixture
async def committed(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_CommittedState]:
    state = _CommittedState(factory)
    monkeypatch.setattr(data_import_service, "session_factory", factory)
    monkeypatch.setattr(audit_service, "session_factory", factory)
    try:
        yield state
    finally:
        await _cleanup(state)


async def _make_definition(
    session: AsyncSession,
    state: _CommittedState,
    owner: User,
) -> DataTableDefinition:
    table = DataTableDefinition(
        name=f"task9-table-{uuid4().hex[:12]}",
        created_by=owner.id,
        status=DataTableStatus.DRAFT,
    )
    session.add(table)
    await session.flush()
    state.track_table(table)
    fields = [
        DataFieldDefinition(
            table_id=table.id,
            label="Business Date",
            field_type=DataFieldType.DATE,
            system_key=DataFieldRole.BUSINESS_DATE,
            required=True,
            sort_order=1,
        ),
        DataFieldDefinition(
            table_id=table.id,
            label="Upstream",
            field_type=DataFieldType.CUSTOMER,
            system_key=DataFieldRole.UPSTREAM_CUSTOMER,
            required=True,
            sort_order=2,
        ),
        DataFieldDefinition(
            table_id=table.id,
            label="Downstream",
            field_type=DataFieldType.CUSTOMER,
            system_key=DataFieldRole.DOWNSTREAM_CUSTOMER,
            required=True,
            sort_order=3,
        ),
        DataFieldDefinition(
            table_id=table.id,
            label="Revenue",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.REVENUE,
            required=True,
            sort_order=4,
        ),
        DataFieldDefinition(
            table_id=table.id,
            label="Cost",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.COST,
            required=True,
            sort_order=5,
        ),
    ]
    session.add_all(fields)
    await session.flush()
    return table


def _context(
    user: User,
    *,
    permissions: set[str] | None = None,
    data_scope: DataScope = DataScope.ALL,
) -> AuthContext:
    return AuthContext(
        user_id=user.id,
        role_id=1,
        department_id=user.department_id,
        data_scope=data_scope,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(
            permissions if permissions is not None else {"data:import"}
        ),
        is_super_admin=False,
        username=user.username,
        nickname=user.nickname,
    )


def _allow_requester(
    monkeypatch: pytest.MonkeyPatch,
    context: AuthContext,
) -> None:
    async def build(session: AsyncSession, user: User) -> AuthContext:
        del session, user
        return context

    monkeypatch.setattr(data_import_service, "build_auth_context", build)


def _json_bytes(rows: list[dict[str, object]]) -> bytes:
    return json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8")


def _row(
    business_date: date,
    upstream_id: int,
    downstream_id: int,
    *,
    revenue: str = "10.25",
    cost: str = "4.75",
) -> dict[str, object]:
    return {
        "business_date": business_date.isoformat(),
        "upstream_customer_id": upstream_id,
        "downstream_customer_id": downstream_id,
        "revenue": revenue,
        "cost": cost,
    }


def _store_json(rows: list[dict[str, object]]) -> StoredImportSource:
    return save_import_bytes(
        _json_bytes(rows),
        suffix=".json",
        original_name="source.json",
    )


async def _enqueue(
    session: AsyncSession,
    table: DataTableDefinition,
    stored: StoredImportSource,
    context: AuthContext,
    *,
    source: DataImportSource = DataImportSource.API,
) -> DataImportJob:
    return await data_import_service.enqueue_import(
        session,
        table.id,
        source=source,
        stored=stored,
        context=context,
    )


async def _claim(
    factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> DataImportJob:
    async with factory() as session:
        job = await data_import_repository.claim_next_import_job(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        assert job is not None
        return job


async def _job(factory: async_sessionmaker[AsyncSession], job_id: int) -> DataImportJob:
    async with factory() as session:
        result = await data_import_repository.get_import_job(session, job_id)
        assert result is not None
        return result


async def _seed_active_snapshot(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    owner: User,
    business_date: date,
    version: int = 1,
) -> DataSnapshot:
    history_job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.API,
        status=DataImportStatus.SUCCEEDED,
        storage_path=f"history/{uuid4().hex}.json",
        content_sha256="a" * 64,
        size_bytes=1,
        requested_by=owner.id,
        progress_percent=100,
        finished_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(history_job)
    await session.flush()
    snapshot = DataSnapshot(
        table_id=table.id,
        business_date=business_date,
        import_id=history_job.id,
        version=version,
        status=DataSnapshotStatus.ACTIVE,
        row_count=1,
        activated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(snapshot)
    await session.flush()
    await session.execute(
        DataCurrentSnapshot.insert().values(
            table_id=table.id,
            business_date=business_date,
            snapshot_id=snapshot.id,
        )
    )
    return snapshot


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession],
    action: str | None = None,
) -> list[AuditLog]:
    async with factory() as session:
        statement = sa.select(AuditLog).order_by(AuditLog.id)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        return list((await session.scalars(statement)).all())


async def test_enqueue_persists_source_and_request_audit(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as session:
        owner = await make_user(session)
        committed.track_user(owner)
        table = await _make_definition(session, committed, owner)
        context = _context(owner)
        stored = _store_json([_row(DATE_ONE, 1, 2)])
        job = await _enqueue(session, table, stored, context)
        assert job.status == DataImportStatus.QUEUED
        assert job.source == DataImportSource.API
        assert job.storage_path == stored.storage_path
        assert job.content_sha256 == stored.content_sha256
        assert job.size_bytes == stored.size_bytes
        assert job.requested_by == owner.id

    audits = await _audit_rows(factory, "data_import:request")
    assert len(audits) == 1
    assert audits[0].resource_type == "custom_data_import"
    assert audits[0].resource_id == job.id
    assert audits[0].actor_username == owner.username
    assert audits[0].result == AuditResult.SUCCESS


async def test_enqueue_rejects_missing_import_permission(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as session:
        owner = await make_user(session)
        committed.track_user(owner)
        table = await _make_definition(session, committed, owner)
        stored = _store_json([_row(DATE_ONE, 1, 2)])

        with pytest.raises(BusinessError) as raised:
            await _enqueue(
                session,
                table,
                stored,
                _context(owner, permissions=set()),
            )

        assert raised.value.code == ErrorCode.FORBIDDEN
        assert raised.value.http_status == 403
        assert await data_import_repository.list_import_jobs(session, table.id) == []

    audits = await _audit_rows(factory, "data_import:request")
    assert len(audits) == 1
    assert audits[0].result == AuditResult.DENIED


async def test_enqueue_concurrent_requests_create_one_job(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        first_source = _store_json([_row(DATE_ONE, 1, 2)])
        second_source = _store_json([_row(DATE_TWO, 1, 2)])
        table_id = table.id
        await setup.commit()

    async def enqueue(stored: StoredImportSource) -> DataImportJob | BusinessError:
        async with factory() as session:
            try:
                return await data_import_service.enqueue_import(
                    session,
                    table_id,
                    source=DataImportSource.API,
                    stored=stored,
                    context=context,
                )
            except BusinessError as error:
                await session.rollback()
                return error

    results = await asyncio.gather(enqueue(first_source), enqueue(second_source))
    jobs = [item for item in results if isinstance(item, DataImportJob)]
    errors = [item for item in results if isinstance(item, BusinessError)]
    assert len(jobs) == 1
    assert [error.code for error in errors] == [ErrorCode.DATA_IMPORT_IN_PROGRESS]


async def test_process_json_import_activates_all_dates_and_preserves_absent_date(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(
            setup,
            owner=owner,
            name=f"Upstream Secret {uuid4().hex[:6]}",
        )
        downstream = await make_customer_row(
            setup,
            owner=owner,
            name=f"Downstream Secret {uuid4().hex[:6]}",
        )
        table = await _make_definition(setup, committed, owner)
        unchanged = await _seed_active_snapshot(
            setup,
            table=table,
            owner=owner,
            business_date=DATE_UNCHANGED,
        )
        unchanged_id = unchanged.id
        stored = _store_json(
            [
                _row(DATE_ONE, upstream.id, downstream.id),
                _row(DATE_ONE, downstream.id, upstream.id, revenue="0", cost="0"),
                _row(DATE_TWO, upstream.id, downstream.id),
            ]
        )
        context = _context(owner)
        job = await _enqueue(setup, table, stored, context)
        job_id = job.id
        table_id = table.id
        await setup.commit()

    def unexpected_json_count(*args: Any, **kwargs: Any) -> int:
        del args, kwargs
        raise AssertionError("JSON row-count prepass must not run")

    _allow_requester(monkeypatch, context)
    monkeypatch.setattr(
        data_import_parser,
        "json_import_row_count",
        unexpected_json_count,
    )
    claimed = await _claim(factory, worker_id="worker-json")
    assert claimed.id == job_id
    await data_import_service.process_import_job(job_id, worker_id="worker-json")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.SUCCEEDED, (
        refreshed.failure_code,
        refreshed.failure_message,
    )
    assert refreshed.total_rows == 3
    assert refreshed.valid_rows == 3
    assert refreshed.invalid_rows == 0
    assert refreshed.error_count == 0
    assert refreshed.date_from == DATE_ONE
    assert refreshed.date_to == DATE_TWO
    assert refreshed.progress_percent == 100

    async with factory() as session:
        current = await data_import_repository.list_current_snapshots(session, table_id)
        assert {snapshot.business_date for snapshot in current} == {
            DATE_ONE,
            DATE_TWO,
            DATE_UNCHANGED,
        }
        unchanged_pointer = await session.scalar(
            sa.select(DataCurrentSnapshot.c.snapshot_id).where(
                DataCurrentSnapshot.c.table_id == table_id,
                DataCurrentSnapshot.c.business_date == DATE_UNCHANGED,
            )
        )
        assert unchanged_pointer == unchanged_id
        records = list(
            (
                await session.scalars(
                    sa.select(DataRecord).where(DataRecord.table_id == table_id)
                )
            ).all()
        )
        assert len(records) == 3
        assert {record.upstream_customer_name_source for record in records} == {
            upstream.name.strip(),
            downstream.name.strip(),
        }
        table_row = await session.get(DataTableDefinition, table_id)
        assert table_row is not None
        assert table_row.status == DataTableStatus.LOCKED

    audit = await _audit_rows(factory, "data_import:complete")
    assert len(audit) == 1
    assert audit[0].resource_id == job_id
    assert audit[0].actor_username == owner.username
    assert audit[0].result == AuditResult.SUCCESS


async def test_process_excel_import_uses_shared_validation_pipeline(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(
            setup,
            owner=owner,
            name=f"Excel Upstream {uuid4().hex[:6]}",
        )
        downstream = await make_customer_row(
            setup,
            owner=owner,
            name=f"Excel Downstream {uuid4().hex[:6]}",
        )
        table = await _make_definition(setup, committed, owner)
        resolved = await load_resolved_definition(setup, table.id)
        assert resolved is not None
        workbook = load_workbook(BytesIO(build_excel_template(resolved)))
        worksheet = workbook.active
        worksheet.append(
            [
                DATE_ONE,
                upstream.name,
                downstream.name,
                "19.50",
                "8.25",
            ]
        )
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        stored = save_import_bytes(
            buffer.getvalue(),
            suffix=".xlsx",
            original_name="source.xlsx",
        )
        context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            stored,
            context,
            source=DataImportSource.EXCEL,
        )
        job_id = job.id
        await setup.commit()

    _allow_requester(monkeypatch, context)
    await _claim(factory, worker_id="worker-excel")
    await data_import_service.process_import_job(job_id, worker_id="worker-excel")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.SUCCEEDED
    assert refreshed.total_rows == 1
    assert refreshed.valid_rows == 1
    assert refreshed.error_count == 0
    async with factory() as session:
        record = await session.scalar(
            sa.select(DataRecord).where(DataRecord.snapshot_id.is_not(None))
        )
        assert record is not None
        assert record.business_date == DATE_ONE
        assert record.upstream_customer_id == upstream.id


async def test_validation_persists_only_first_10000_error_details(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(setup, owner=owner)
        downstream = await make_customer_row(setup, owner=owner)
        table = await _make_definition(setup, committed, owner)
        invalid_rows = []
        for _ in range(10_005):
            row = _row(DATE_ONE, upstream.id, downstream.id)
            row["business_date"] = "not-a-date"
            invalid_rows.append(row)
        stored = _store_json(invalid_rows)
        context = _context(owner)
        job = await _enqueue(setup, table, stored, context)
        job_id = job.id
        table_id = table.id
        await setup.commit()

    _allow_requester(monkeypatch, context)
    await _claim(factory, worker_id="worker-errors")
    await data_import_service.process_import_job(job_id, worker_id="worker-errors")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.total_rows == 10_005
    assert refreshed.valid_rows == 0
    assert refreshed.invalid_rows == 10_005
    assert refreshed.error_count == 10_005
    assert refreshed.date_from is None
    assert refreshed.date_to is None
    async with factory() as session:
        details = await data_import_repository.list_import_errors(
            session,
            job_id,
            limit=20_000,
        )
        assert len(details) == 10_000
        assert {detail.error_code for detail in details} == {"invalid_date"}
        assert (
            await data_import_repository.list_current_snapshots(session, table_id) == []
        )


async def test_multi_date_validation_error_keeps_all_existing_pointers_unchanged(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(setup, owner=owner)
        downstream = await make_customer_row(setup, owner=owner)
        table = await _make_definition(setup, committed, owner)
        first = await _seed_active_snapshot(
            setup,
            table=table,
            owner=owner,
            business_date=DATE_ONE,
        )
        third = await _seed_active_snapshot(
            setup,
            table=table,
            owner=owner,
            business_date=DATE_UNCHANGED,
        )
        first_id = first.id
        third_id = third.id
        stored = _store_json(
            [
                _row(DATE_ONE, upstream.id, downstream.id),
                _row(DATE_TWO, upstream.id, 2**63 - 1),
            ]
        )
        context = _context(owner)
        job = await _enqueue(setup, table, stored, context)
        job_id = job.id
        table_id = table.id
        await setup.commit()

    _allow_requester(monkeypatch, context)
    await _claim(factory, worker_id="worker-atomic")
    await data_import_service.process_import_job(job_id, worker_id="worker-atomic")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.error_count == 1
    assert refreshed.invalid_rows == 1
    async with factory() as session:
        pointers = {
            business_date: snapshot_id
            for business_date, snapshot_id in (
                await session.execute(
                    sa.select(
                        DataCurrentSnapshot.c.business_date,
                        DataCurrentSnapshot.c.snapshot_id,
                    ).where(DataCurrentSnapshot.c.table_id == table_id)
                )
            ).all()
        }
        assert pointers == {DATE_ONE: first_id, DATE_UNCHANGED: third_id}
        staged = list(
            (
                await session.scalars(
                    sa.select(DataSnapshot).where(
                        DataSnapshot.table_id == table_id,
                        DataSnapshot.status == DataSnapshotStatus.STAGED,
                    )
                )
            ).all()
        )
        assert staged == []
        table_row = await session.get(DataTableDefinition, table_id)
        assert table_row is not None
        assert table_row.status == DataTableStatus.DRAFT


async def test_failure_after_staging_retains_cleanup_identity_and_discards_staged_rows(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(setup, owner=owner)
        downstream = await make_customer_row(setup, owner=owner)
        table = await _make_definition(setup, committed, owner)
        stored = _store_json([_row(DATE_ONE, upstream.id, downstream.id)])
        context = _context(owner)
        job = await _enqueue(setup, table, stored, context)
        job_id = job.id
        table_id = table.id
        source_path = job.storage_path
        await setup.commit()

    _allow_requester(monkeypatch, context)
    claimed = await _claim(factory, worker_id="worker-failure", lease_seconds=60)
    attempt_count = claimed.attempt_count

    async def fail_activation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("sensitive internal details")

    monkeypatch.setattr(data_import_service, "activate_import", fail_activation)
    await data_import_service.process_import_job(job_id, worker_id="worker-failure")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.lease_owner == "worker-failure"
    assert refreshed.attempt_count == attempt_count
    assert refreshed.storage_path == source_path
    assert refreshed.failure_message is not None
    assert "sensitive" not in refreshed.failure_message
    async with factory() as session:
        snapshots = list(
            (
                await session.scalars(
                    sa.select(DataSnapshot).where(DataSnapshot.table_id == table_id)
                )
            ).all()
        )
        assert snapshots == []
        table_row = await session.get(DataTableDefinition, table_id)
        assert table_row is not None
        assert table_row.status == DataTableStatus.DRAFT
    failed_audit = await _audit_rows(factory, "data_import:fail")
    assert len(failed_audit) == 1
    assert failed_audit[0].resource_id == job_id
    assert failed_audit[0].actor_username == owner.username
    assert failed_audit[0].result == AuditResult.DENIED


async def test_reclaimed_attempt_cannot_write_stage_fail_or_cleanup_new_state(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    monkeypatch.setattr(data_import_service, "_LEASE_SECONDS", 1)
    monkeypatch.setattr(data_import_service, "_LEASE_RENEW_INTERVAL_SECONDS", 3600.0)
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(setup, owner=owner)
        downstream = await make_customer_row(setup, owner=owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        invalid_row = _row(
            DATE_ONE,
            upstream.id,
            downstream.id,
            revenue="not-a-decimal",
        )
        job = await _enqueue(
            setup,
            table,
            _store_json([invalid_row]),
            context,
        )
        job_id = job.id
        table_id = table.id
        await setup.commit()

    _allow_requester(monkeypatch, context)
    old_claim = await _claim(factory, worker_id="worker-old", lease_seconds=1)
    old_identity = data_import_service._claim_identity(old_claim, "worker-old")
    paused = asyncio.Event()
    release = asyncio.Event()
    original_persist = data_import_service._persist_validation_summary

    async def paused_persist(*args: Any, **kwargs: Any) -> None:
        paused.set()
        await release.wait()
        await original_persist(*args, **kwargs)

    monkeypatch.setattr(
        data_import_service,
        "_persist_validation_summary",
        paused_persist,
    )
    old_task = asyncio.create_task(
        data_import_service.process_import_job(job_id, worker_id="worker-old")
    )
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        await asyncio.sleep(1.1)
        new_claim = await _claim(
            factory,
            worker_id="worker-new",
            lease_seconds=120,
        )
        assert new_claim.id == job_id
        assert new_claim.attempt_count == old_claim.attempt_count + 1

        async with factory() as session:
            new_job = await session.get(DataImportJob, job_id)
            assert new_job is not None
            new_job.total_rows = 41
            new_job.valid_rows = 40
            new_job.invalid_rows = 1
            new_job.error_count = 3
            new_job.date_from = DATE_ONE
            new_job.date_to = DATE_ONE
            new_job.progress_percent = 61
            session.add(
                DataImportError(
                    import_id=job_id,
                    row_number=41,
                    error_code="new_attempt_error",
                    message="new-attempt-diagnostic",
                )
            )
            await session.commit()

        async with factory() as session:
            new_snapshots = await data_import_service.start_staged_import(
                session,
                job_id,
                {DATE_ONE},
                worker_id="worker-new",
            )
        new_snapshot_id = new_snapshots[DATE_ONE].id

        release.set()
        await asyncio.wait_for(old_task, timeout=5)
    finally:
        release.set()
        if not old_task.done():
            old_task.cancel()
        with suppress(asyncio.CancelledError):
            await old_task

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.APPLYING
    assert refreshed.lease_owner == "worker-new"
    assert refreshed.attempt_count == new_claim.attempt_count
    assert refreshed.total_rows == 41
    assert refreshed.valid_rows == 40
    assert refreshed.invalid_rows == 1
    assert refreshed.error_count == 3
    assert refreshed.date_from == DATE_ONE
    assert refreshed.date_to == DATE_ONE
    assert refreshed.progress_percent == 61
    assert refreshed.failure_code is None
    assert refreshed.failure_message is None
    assert refreshed.finished_at is None

    async with factory() as session:
        errors = await data_import_repository.list_import_errors(session, job_id)
        assert [error.error_code for error in errors] == ["new_attempt_error"]
        snapshot = await session.get(DataSnapshot, new_snapshot_id)
        assert snapshot is not None
        assert snapshot.status == DataSnapshotStatus.STAGED
        assert snapshot.row_count == 0
        records = list(
            (
                await session.scalars(
                    sa.select(DataRecord).where(DataRecord.table_id == table_id)
                )
            ).all()
        )
        assert records == []

    with pytest.raises(BusinessError):
        async with factory() as session:
            await data_import_service.start_staged_import(
                session,
                job_id,
                {DATE_ONE},
                worker_id="worker-old",
            )
    with pytest.raises(BusinessError):
        async with factory() as session:
            await data_import_service.append_staged_rows(
                session,
                new_snapshot_id,
                [],
                worker_id="worker-old",
            )
    with pytest.raises(BusinessError):
        async with factory() as session:
            await data_import_service.activate_import(
                session,
                import_id=job_id,
                worker_id="worker-old",
                expected_dates={DATE_ONE},
            )
    with pytest.raises(BusinessError):
        async with factory() as session:
            await data_import_service.discard_staged_import(
                session,
                job_id,
                worker_id="worker-old",
                attempt_count=old_claim.attempt_count,
            )
    assert not await data_import_service._mark_failed_and_discard(
        old_identity,
        code="STALE",
        message="stale worker",
    )

    final_state = await _job(factory, job_id)
    assert final_state.status == DataImportStatus.APPLYING
    assert final_state.lease_owner == "worker-new"
    assert final_state.attempt_count == new_claim.attempt_count
    assert final_state.progress_percent == 61
    async with factory() as session:
        assert await session.get(DataSnapshot, new_snapshot_id) is not None


async def test_process_requires_current_matching_lease(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            _store_json([_row(DATE_ONE, 1, 2)]),
            context,
        )
        job_id = job.id
        await setup.commit()

    with pytest.raises(BusinessError) as unclaimed:
        await data_import_service.process_import_job(job_id, worker_id="worker-a")
    assert unclaimed.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert (await _job(factory, job_id)).status == DataImportStatus.QUEUED

    claimed = await _claim(factory, worker_id="worker-a")
    assert claimed.id == job_id
    with pytest.raises(BusinessError) as foreign:
        await data_import_service.process_import_job(job_id, worker_id="worker-b")
    assert foreign.value.code == ErrorCode.DATA_IMPORT_INVALID
    still_claimed = await _job(factory, job_id)
    assert still_claimed.status == DataImportStatus.VALIDATING
    assert still_claimed.lease_owner == "worker-a"


async def test_process_fails_when_requester_becomes_inactive(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            _store_json([_row(DATE_ONE, 1, 2)]),
            context,
        )
        job_id = job.id
        owner.status = UserStatus.DISABLED
        await setup.commit()

    await _claim(factory, worker_id="worker-disabled")
    await data_import_service.process_import_job(job_id, worker_id="worker-disabled")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.failure_code == ErrorCode.ACCOUNT_DISABLED.name


async def test_process_fails_when_requester_loses_import_permission(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        table = await _make_definition(setup, committed, owner)
        enqueue_context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            _store_json([_row(DATE_ONE, 1, 2)]),
            enqueue_context,
        )
        job_id = job.id
        await setup.commit()

    _allow_requester(monkeypatch, _context(owner, permissions=set()))
    await _claim(factory, worker_id="worker-permission")
    await data_import_service.process_import_job(job_id, worker_id="worker-permission")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.failure_code == ErrorCode.FORBIDDEN.name


async def test_limited_scope_rejects_invisible_counterparty_without_leaking_values(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        other_owner = await make_user(setup)
        committed.track_user(owner)
        committed.track_user(other_owner)
        visible = await make_customer_row(
            setup,
            owner=owner,
            name=f"Visible Customer {uuid4().hex[:8]}",
        )
        invisible = await make_customer_row(
            setup,
            owner=other_owner,
            name=f"Hidden Customer {uuid4().hex[:8]}",
        )
        table = await _make_definition(setup, committed, owner)
        enqueue_context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            _store_json([_row(DATE_ONE, visible.id, invisible.id)]),
            enqueue_context,
        )
        job_id = job.id
        await setup.commit()

    restricted_context = _context(owner, data_scope=DataScope.SELF)
    _allow_requester(monkeypatch, restricted_context)
    await _claim(factory, worker_id="worker-scope")
    await data_import_service.process_import_job(job_id, worker_id="worker-scope")

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.error_count == 1
    assert refreshed.invalid_rows == 1
    async with factory() as session:
        errors = await data_import_repository.list_import_errors(session, job_id)
        assert len(errors) == 1
        assert errors[0].error_code == "customer_out_of_scope"
        serialized = " ".join(
            part or ""
            for error in errors
            for part in (error.message, error.column_label, error.sheet_name)
        )
        assert invisible.name.strip() not in serialized
        assert visible.name.strip() not in serialized
    audits = await _audit_rows(factory)
    serialized_audits = " ".join(
        part or ""
        for audit in audits
        for part in (audit.action, audit.reason, audit.resource_type)
    )
    assert invisible.name.strip() not in serialized_audits
    assert visible.name.strip() not in serialized_audits


async def test_long_parse_renews_lease_periodically(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    monkeypatch.setattr(data_import_service, "_LEASE_SECONDS", 2)
    monkeypatch.setattr(data_import_service, "_LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(setup, owner=owner)
        downstream = await make_customer_row(setup, owner=owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            _store_json([_row(DATE_ONE, upstream.id, downstream.id)]),
            context,
        )
        job_id = job.id
        await setup.commit()

    original_iter = data_import_parser.iter_json_rows

    def slow_iter(*args: Any, **kwargs: Any) -> Iterator[Any]:
        time.sleep(0.08)
        yield from original_iter(*args, **kwargs)

    monkeypatch.setattr(data_import_parser, "iter_json_rows", slow_iter)
    renewals: list[bool] = []
    original_renew = data_import_repository.renew_import_lease

    async def tracked_renew(
        session: AsyncSession,
        *,
        import_id: int,
        worker_id: str,
        lease_seconds: int,
        attempt_count: int | None = None,
    ) -> bool:
        renewed = await original_renew(
            session,
            import_id=import_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            attempt_count=attempt_count,
        )
        renewals.append(renewed)
        return renewed

    monkeypatch.setattr(
        data_import_repository,
        "renew_import_lease",
        tracked_renew,
    )
    original_rehydrate = data_import_service._rehydrate_requester
    renewals_before_requester: list[int] = []

    async def tracked_rehydrate(
        session: AsyncSession,
        requested_by: int,
    ) -> AuthContext:
        renewals_before_requester.append(len(renewals))
        return await original_rehydrate(session, requested_by)

    monkeypatch.setattr(
        data_import_service,
        "_rehydrate_requester",
        tracked_rehydrate,
    )
    _allow_requester(monkeypatch, context)
    await _claim(factory, worker_id="worker-lease", lease_seconds=2)
    await data_import_service.process_import_job(job_id, worker_id="worker-lease")

    assert renewals_before_requester == [1]
    assert len(renewals) >= 2
    assert all(renewals)
    assert (await _job(factory, job_id)).status == DataImportStatus.SUCCEEDED


async def test_restart_claim_cleans_old_staged_snapshot_before_reprocessing(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        upstream = await make_customer_row(setup, owner=owner)
        downstream = await make_customer_row(setup, owner=owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        stored = _store_json([_row(DATE_ONE, upstream.id, downstream.id)])
        now = datetime.now(UTC).replace(tzinfo=None)
        job = DataImportJob(
            table_id=table.id,
            source=DataImportSource.API,
            status=DataImportStatus.APPLYING,
            storage_path=stored.storage_path,
            original_filename=stored.original_name,
            content_sha256=stored.content_sha256,
            size_bytes=stored.size_bytes,
            requested_by=owner.id,
            attempt_count=1,
            lease_owner="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=1),
            started_at=now - timedelta(minutes=5),
            total_rows=1,
            valid_rows=1,
            progress_percent=50,
        )
        setup.add(job)
        await setup.flush()
        old_snapshot = DataSnapshot(
            table_id=table.id,
            business_date=DATE_ONE,
            import_id=job.id,
            version=1,
            status=DataSnapshotStatus.STAGED,
            row_count=1,
        )
        setup.add(old_snapshot)
        await setup.flush()
        old_record = DataRecord(
            snapshot_id=old_snapshot.id,
            table_id=table.id,
            business_date=DATE_ONE,
            upstream_customer_id=upstream.id,
            downstream_customer_id=downstream.id,
            upstream_customer_name_source=upstream.name,
            downstream_customer_name_source=downstream.name,
            revenue=1,
            cost=1,
        )
        setup.add(old_record)
        await setup.flush()
        job_id = job.id
        table_id = table.id
        old_snapshot_id = old_snapshot.id
        old_record_id = old_record.id
        await setup.commit()

    _allow_requester(monkeypatch, context)
    reclaimed = await _claim(factory, worker_id="recovery-worker")
    assert reclaimed.id == job_id
    async with factory() as session:
        assert await session.get(DataSnapshot, old_snapshot_id) is None
        assert await session.get(DataRecord, old_record_id) is None
    await data_import_service.process_import_job(
        job_id,
        worker_id="recovery-worker",
    )

    refreshed = await _job(factory, job_id)
    assert refreshed.status == DataImportStatus.SUCCEEDED
    assert refreshed.attempt_count == 2
    async with factory() as session:
        active = await data_import_repository.list_current_snapshots(session, table_id)
        assert len(active) == 1
        assert active[0].business_date == DATE_ONE
        assert active[0].id != old_snapshot_id
