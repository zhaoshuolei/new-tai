"""Snapshot staging, versioning, activation, and cleanup tests."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
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
from app.models.user import User
from app.services import data_import as data_import_service
from app.services.data_import_parser import FieldValue, ValidatedImportRow
from tests.customer_helpers import TEST_DATABASE_URL, make_customer_row, make_user

BUSINESS_DATE = date(2026, 10, 18)
SECOND_DATE = date(2026, 10, 19)
UNCHANGED_DATE = date(2026, 10, 20)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


async def _make_table(session: AsyncSession, owner: User) -> DataTableDefinition:
    table = DataTableDefinition(
        name=f"snapshot-table-{uuid4().hex[:12]}",
        created_by=owner.id,
    )
    session.add(table)
    await session.flush()
    return table


async def _make_job(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    owner: User,
    status: DataImportStatus = DataImportStatus.VALIDATING,
    worker_id: str = "test-worker",
    expired: bool = False,
    attempt_count: int = 1,
) -> DataImportJob:
    now = datetime.now(UTC).replace(tzinfo=None)
    job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.API,
        status=status,
        storage_path=f"imports/{uuid4().hex}.json",
        content_sha256=uuid4().hex + uuid4().hex,
        size_bytes=100,
        requested_by=owner.id,
        attempt_count=attempt_count,
        lease_owner=worker_id
        if status
        in {
            DataImportStatus.VALIDATING,
            DataImportStatus.APPLYING,
        }
        else None,
        lease_expires_at=(
            now - timedelta(minutes=1) if expired else now + timedelta(minutes=5)
        )
        if status in {DataImportStatus.VALIDATING, DataImportStatus.APPLYING}
        else None,
        heartbeat_at=now
        if status in {DataImportStatus.VALIDATING, DataImportStatus.APPLYING}
        else None,
        started_at=now
        if status in {DataImportStatus.VALIDATING, DataImportStatus.APPLYING}
        else None,
    )
    session.add(job)
    await session.flush()
    return job


async def _add_snapshot(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    job: DataImportJob,
    business_date: date,
    version: int,
    status: DataSnapshotStatus,
) -> DataSnapshot:
    snapshot = DataSnapshot(
        table_id=table.id,
        business_date=business_date,
        import_id=job.id,
        version=version,
        status=status,
        activated_at=datetime.now(UTC).replace(tzinfo=None)
        if status != DataSnapshotStatus.STAGED
        else None,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def _add_record(
    session: AsyncSession,
    *,
    snapshot: DataSnapshot,
    customer_id: int,
    customer_name: str,
) -> DataRecord:
    record = DataRecord(
        snapshot_id=snapshot.id,
        table_id=snapshot.table_id,
        business_date=snapshot.business_date,
        upstream_customer_id=customer_id,
        downstream_customer_id=customer_id,
        upstream_customer_name_source=customer_name,
        downstream_customer_name_source=customer_name,
        revenue=Decimal("10.25"),
        cost=Decimal("4.75"),
    )
    session.add(record)
    await session.flush()
    return record


async def _point(
    session: AsyncSession,
    *,
    table_id: int,
    business_date: date,
    snapshot_id: int,
) -> None:
    await session.execute(
        DataCurrentSnapshot.insert().values(
            table_id=table_id,
            business_date=business_date,
            snapshot_id=snapshot_id,
        )
    )
    await session.flush()


async def _custom_field(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    field_type: DataFieldType,
    label: str,
    options: tuple[str, ...] = (),
) -> tuple[DataFieldDefinition, list[DataEnumOption]]:
    field = DataFieldDefinition(
        table_id=table.id,
        label=label,
        field_type=field_type,
        system_key=DataFieldRole.CUSTOM,
        required=False,
        sort_order=10,
    )
    session.add(field)
    await session.flush()
    enum_options = [
        DataEnumOption(field_id=field.id, label=option, sort_order=index)
        for index, option in enumerate(options)
    ]
    if enum_options:
        session.add_all(enum_options)
        await session.flush()
    return field, enum_options


def _validated_row(
    *,
    customer_id: int,
    customer_name: str,
    business_date: date,
    custom_values: dict[int, FieldValue],
) -> ValidatedImportRow:
    return ValidatedImportRow(
        business_date=business_date,
        upstream_customer_id=customer_id,
        downstream_customer_id=customer_id,
        upstream_customer_name_source=customer_name,
        downstream_customer_name_source=customer_name,
        revenue=Decimal("10.25"),
        cost=Decimal("4.75"),
        custom_values=custom_values,
    )


async def test_append_staged_rows_persists_all_typed_values_in_batches(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    text_field, _ = await _custom_field(
        session, table=table, field_type=DataFieldType.TEXT, label="Text"
    )
    integer_field, _ = await _custom_field(
        session, table=table, field_type=DataFieldType.INTEGER, label="Integer"
    )
    decimal_field, _ = await _custom_field(
        session, table=table, field_type=DataFieldType.DECIMAL, label="Decimal"
    )
    date_field, _ = await _custom_field(
        session, table=table, field_type=DataFieldType.DATE, label="Date"
    )
    boolean_field, _ = await _custom_field(
        session, table=table, field_type=DataFieldType.BOOLEAN, label="Boolean"
    )
    enum_field, enum_options = await _custom_field(
        session,
        table=table,
        field_type=DataFieldType.ENUM,
        label="Enum",
        options=("Alpha", "Beta"),
    )
    job = await _make_job(session, table=table, owner=owner)
    snapshots = await data_import_service.start_staged_import(
        session, job, {BUSINESS_DATE}
    )
    field_values = {
        text_field.id: FieldValue(field_id=text_field.id, text_value="hello"),
        integer_field.id: FieldValue(field_id=integer_field.id, integer_value=42),
        decimal_field.id: FieldValue(
            field_id=decimal_field.id, decimal_value=Decimal("1.25000000")
        ),
        date_field.id: FieldValue(field_id=date_field.id, date_value=BUSINESS_DATE),
        boolean_field.id: FieldValue(field_id=boolean_field.id, boolean_value=True),
        enum_field.id: FieldValue(
            field_id=enum_field.id, enum_option_id=enum_options[1].id
        ),
    }
    row = _validated_row(
        customer_id=customer.id,
        customer_name=customer.name,
        business_date=BUSINESS_DATE,
        custom_values=field_values,
    )

    await data_import_service.append_staged_rows(
        session,
        snapshots[BUSINESS_DATE].id,
        [row, row],
    )

    record_rows = list(
        (
            await session.scalars(
                sa.select(DataRecord).where(
                    DataRecord.snapshot_id == snapshots[BUSINESS_DATE].id
                )
            )
        ).all()
    )
    assert len(record_rows) == 2
    values = list(
        (
            await session.scalars(
                sa.select(DataRecordValue).where(
                    DataRecordValue.record_id.in_([item.id for item in record_rows])
                )
            )
        ).all()
    )
    assert len(values) == 12
    by_record = {
        record.id: {
            value.field_id: value for value in values if value.record_id == record.id
        }
        for record in record_rows
    }
    for typed_values in by_record.values():
        assert typed_values[text_field.id].text_value == "hello"
        assert typed_values[integer_field.id].integer_value == 42
        assert typed_values[decimal_field.id].decimal_value == Decimal("1.25000000")
        assert typed_values[date_field.id].date_value == BUSINESS_DATE
        assert typed_values[boolean_field.id].boolean_value is True
        assert typed_values[enum_field.id].enum_option_id == enum_options[1].id
    refreshed_snapshot = await session.get(DataSnapshot, snapshots[BUSINESS_DATE].id)
    assert refreshed_snapshot is not None
    assert refreshed_snapshot.row_count == 2


async def test_start_allocates_versions_above_all_history(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    history_job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    await _add_snapshot(
        session,
        table=table,
        job=history_job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
    )
    await _add_snapshot(
        session,
        table=table,
        job=history_job,
        business_date=BUSINESS_DATE,
        version=2,
        status=DataSnapshotStatus.SUPERSEDED,
    )
    job = await _make_job(session, table=table, owner=owner)

    snapshots = await data_import_service.start_staged_import(
        session, job, {BUSINESS_DATE}
    )

    assert snapshots[BUSINESS_DATE].version == 3
    assert snapshots[BUSINESS_DATE].status == DataSnapshotStatus.STAGED


async def test_activation_switches_all_dates_and_preserves_unlisted_date(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    old_job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    old_snapshots = {
        business_date: await _add_snapshot(
            session,
            table=table,
            job=old_job,
            business_date=business_date,
            version=1,
            status=DataSnapshotStatus.ACTIVE,
        )
        for business_date in (BUSINESS_DATE, SECOND_DATE, UNCHANGED_DATE)
    }
    old_records = {
        business_date: await _add_record(
            session,
            snapshot=snapshot,
            customer_id=customer.id,
            customer_name=customer.name,
        )
        for business_date, snapshot in old_snapshots.items()
    }
    for business_date, snapshot in old_snapshots.items():
        await _point(
            session,
            table_id=table.id,
            business_date=business_date,
            snapshot_id=snapshot.id,
        )
    job = await _make_job(session, table=table, owner=owner)
    staged = await data_import_service.start_staged_import(
        session, job, {BUSINESS_DATE, SECOND_DATE}
    )
    for snapshot in staged.values():
        await data_import_service.append_staged_rows(
            session,
            snapshot.id,
            [
                _validated_row(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    business_date=snapshot.business_date,
                    custom_values={},
                )
            ],
        )

    await data_import_service.activate_import(
        session,
        import_id=job.id,
        worker_id="test-worker",
    )

    current = {
        item.business_date: item
        for item in (
            await session.scalars(
                sa.select(DataSnapshot)
                .join(
                    DataCurrentSnapshot,
                    DataCurrentSnapshot.c.snapshot_id == DataSnapshot.id,
                )
                .where(DataSnapshot.table_id == table.id)
            )
        ).all()
    }
    assert current[BUSINESS_DATE].id == staged[BUSINESS_DATE].id
    assert current[SECOND_DATE].id == staged[SECOND_DATE].id
    assert current[UNCHANGED_DATE].id == old_snapshots[UNCHANGED_DATE].id
    for business_date in (BUSINESS_DATE, SECOND_DATE):
        old = await session.get(DataSnapshot, old_snapshots[business_date].id)
        new = await session.get(DataSnapshot, staged[business_date].id)
        assert old is not None and old.status == DataSnapshotStatus.SUPERSEDED
        assert new is not None and new.status == DataSnapshotStatus.ACTIVE
        assert new.activated_at is not None
        assert await session.get(DataRecord, old_records[business_date].id) is not None
    refreshed_job = await session.get(DataImportJob, job.id)
    refreshed_table = await session.get(DataTableDefinition, table.id)
    assert refreshed_job is not None
    assert refreshed_job.status == DataImportStatus.SUCCEEDED
    assert refreshed_job.finished_at is not None
    assert refreshed_table is not None
    assert refreshed_table.status == DataTableStatus.LOCKED
    assert refreshed_table.locked_at is not None


async def test_start_retry_discards_only_previous_staged_rows(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(session, table=table, owner=owner)
    first = await data_import_service.start_staged_import(session, job, {BUSINESS_DATE})
    first_id = first[BUSINESS_DATE].id
    await data_import_service.append_staged_rows(
        session,
        first_id,
        [
            _validated_row(
                customer_id=customer.id,
                customer_name=customer.name,
                business_date=BUSINESS_DATE,
                custom_values={},
            )
        ],
    )
    first_record_ids = list(
        (
            await session.scalars(
                sa.select(DataRecord.id).where(DataRecord.snapshot_id == first_id)
            )
        ).all()
    )

    second = await data_import_service.start_staged_import(
        session, job, {BUSINESS_DATE}
    )

    assert second[BUSINESS_DATE].id != first_id
    assert await session.get(DataSnapshot, first_id) is None
    assert first_record_ids
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataRecord)
            .where(DataRecord.id.in_(first_record_ids))
        )
        == 0
    )


async def test_discard_removes_only_staged_snapshots_and_rows(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    active_job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    history_job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    staged_job = await _make_job(session, table=table, owner=owner)
    active = await _add_snapshot(
        session,
        table=table,
        job=active_job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
    )
    superseded = await _add_snapshot(
        session,
        table=table,
        job=history_job,
        business_date=SECOND_DATE,
        version=1,
        status=DataSnapshotStatus.SUPERSEDED,
    )
    staged = await _add_snapshot(
        session,
        table=table,
        job=staged_job,
        business_date=UNCHANGED_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    active_record = await _add_record(
        session,
        snapshot=active,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    superseded_record = await _add_record(
        session,
        snapshot=superseded,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    staged_record = await _add_record(
        session,
        snapshot=staged,
        customer_id=customer.id,
        customer_name=customer.name,
    )

    await data_import_service.discard_staged_import(session, staged_job.id)

    assert await session.get(DataSnapshot, staged.id) is None
    assert await session.get(DataRecord, staged_record.id) is None
    assert await session.get(DataSnapshot, active.id) is not None
    assert await session.get(DataSnapshot, superseded.id) is not None
    assert await session.get(DataRecord, active_record.id) is not None
    assert await session.get(DataRecord, superseded_record.id) is not None


async def test_activation_failure_rolls_back_every_pointer_and_status(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    old_job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    old_snapshots = {
        business_date: await _add_snapshot(
            session,
            table=table,
            job=old_job,
            business_date=business_date,
            version=1,
            status=DataSnapshotStatus.ACTIVE,
        )
        for business_date in (BUSINESS_DATE, SECOND_DATE)
    }
    for business_date, snapshot in old_snapshots.items():
        await _point(
            session,
            table_id=table.id,
            business_date=business_date,
            snapshot_id=snapshot.id,
        )
    job = await _make_job(session, table=table, owner=owner)
    staged = await data_import_service.start_staged_import(
        session, job, {BUSINESS_DATE, SECOND_DATE}
    )
    table_id = table.id
    job_id = job.id
    old_snapshot_ids = {
        business_date: snapshot.id for business_date, snapshot in old_snapshots.items()
    }
    staged_snapshot_ids = {
        business_date: snapshot.id for business_date, snapshot in staged.items()
    }

    real_upsert: Any = data_import_service._upsert_current_pointer
    calls = 0

    async def fail_second_pointer(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        await real_upsert(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected pointer failure")

    monkeypatch.setattr(
        data_import_service,
        "_upsert_current_pointer",
        fail_second_pointer,
    )

    with pytest.raises(RuntimeError, match="injected pointer failure"):
        await data_import_service.activate_import(
            session,
            import_id=job_id,
            worker_id="test-worker",
        )

    pointers = {
        row.business_date: row.snapshot_id
        for row in (
            await session.execute(
                sa.select(DataCurrentSnapshot).where(
                    DataCurrentSnapshot.c.table_id == table_id
                )
            )
        ).all()
    }
    assert pointers == old_snapshot_ids
    for business_date, old_snapshot_id in old_snapshot_ids.items():
        refreshed_old = await session.get(DataSnapshot, old_snapshot_id)
        refreshed_new = await session.get(
            DataSnapshot, staged_snapshot_ids[business_date]
        )
        assert refreshed_old is not None
        assert refreshed_old.status == DataSnapshotStatus.ACTIVE
        assert refreshed_new is not None
        assert refreshed_new.status == DataSnapshotStatus.STAGED
    refreshed_job = await session.get(DataImportJob, job_id)
    refreshed_table = await session.get(DataTableDefinition, table_id)
    assert refreshed_job is not None
    assert refreshed_job.status == DataImportStatus.APPLYING
    assert refreshed_table is not None
    assert refreshed_table.status == DataTableStatus.DRAFT


@pytest.mark.parametrize(
    ("worker_id", "expired", "expected_code"),
    [
        ("other-worker", False, ErrorCode.DATA_IMPORT_INVALID),
        ("test-worker", True, ErrorCode.DATA_IMPORT_INVALID),
    ],
)
async def test_activation_rejects_wrong_or_expired_lease(
    session: AsyncSession,
    worker_id: str,
    expired: bool,
    expected_code: ErrorCode,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        worker_id="test-worker",
    )
    await data_import_service.start_staged_import(session, job, {BUSINESS_DATE})
    if expired:
        job.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        await session.flush()
        await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.activate_import(
            session,
            import_id=job.id,
            worker_id=worker_id,
        )

    assert error.value.code == expected_code


async def test_append_rejects_rows_for_a_different_business_date(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(session, table=table, owner=owner)
    snapshot = (
        await data_import_service.start_staged_import(session, job, {BUSINESS_DATE})
    )[BUSINESS_DATE]

    with pytest.raises(BusinessError) as error:
        await data_import_service.append_staged_rows(
            session,
            snapshot.id,
            [
                _validated_row(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    business_date=SECOND_DATE,
                    custom_values={},
                )
            ],
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
