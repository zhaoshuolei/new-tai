"""Snapshot staging, versioning, activation, and cleanup tests."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _delete_committed_table(
    factory: async_sessionmaker[AsyncSession],
    *,
    table_id: int,
    user_id: int,
) -> None:
    async with factory() as cleanup:
        record_ids = sa.select(DataRecord.id).where(DataRecord.table_id == table_id)
        await cleanup.execute(
            sa.delete(DataRecordValue).where(DataRecordValue.record_id.in_(record_ids))
        )
        await cleanup.execute(
            sa.delete(DataRecord).where(DataRecord.table_id == table_id)
        )
        await cleanup.execute(
            DataCurrentSnapshot.delete().where(
                DataCurrentSnapshot.c.table_id == table_id
            )
        )
        await cleanup.execute(
            sa.delete(DataSnapshot).where(DataSnapshot.table_id == table_id)
        )
        await cleanup.execute(
            sa.delete(DataImportJob).where(DataImportJob.table_id == table_id)
        )
        field_ids = sa.select(DataFieldDefinition.id).where(
            DataFieldDefinition.table_id == table_id
        )
        await cleanup.execute(
            sa.delete(DataEnumOption).where(DataEnumOption.field_id.in_(field_ids))
        )
        await cleanup.execute(
            sa.delete(DataFieldDefinition).where(
                DataFieldDefinition.table_id == table_id
            )
        )
        await cleanup.execute(
            sa.delete(DataTableDefinition).where(DataTableDefinition.id == table_id)
        )
        await cleanup.execute(sa.delete(User).where(User.id == user_id))
        await cleanup.commit()


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
        session,
        job.id,
        {BUSINESS_DATE},
        worker_id="test-worker",
    )
    rows = [
        _validated_row(
            customer_id=customer.id,
            customer_name=customer.name,
            business_date=BUSINESS_DATE,
            custom_values={
                text_field.id: FieldValue(field_id=text_field.id, text_value="hello"),
                integer_field.id: FieldValue(
                    field_id=integer_field.id, integer_value=42
                ),
                decimal_field.id: FieldValue(
                    field_id=decimal_field.id,
                    decimal_value=Decimal("1.25000000"),
                ),
                date_field.id: FieldValue(
                    field_id=date_field.id, date_value=BUSINESS_DATE
                ),
                boolean_field.id: FieldValue(
                    field_id=boolean_field.id, boolean_value=True
                ),
                enum_field.id: FieldValue(
                    field_id=enum_field.id, enum_option_id=enum_options[1].id
                ),
            },
        ),
        _validated_row(
            customer_id=customer.id,
            customer_name=customer.name,
            business_date=BUSINESS_DATE,
            custom_values={
                text_field.id: FieldValue(field_id=text_field.id, text_value=""),
                integer_field.id: FieldValue(
                    field_id=integer_field.id, integer_value=0
                ),
                decimal_field.id: FieldValue(
                    field_id=decimal_field.id,
                    decimal_value=Decimal("0.00000000"),
                ),
                date_field.id: FieldValue(
                    field_id=date_field.id, date_value=BUSINESS_DATE
                ),
                boolean_field.id: FieldValue(
                    field_id=boolean_field.id, boolean_value=False
                ),
                enum_field.id: FieldValue(
                    field_id=enum_field.id, enum_option_id=enum_options[0].id
                ),
            },
        ),
    ]

    await data_import_service.append_staged_rows(
        session,
        snapshots[BUSINESS_DATE].id,
        rows,
        worker_id="test-worker",
    )

    record_rows = list(
        (
            await session.scalars(
                sa.select(DataRecord)
                .where(DataRecord.snapshot_id == snapshots[BUSINESS_DATE].id)
                .order_by(DataRecord.id)
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
    ordered_values = [by_record[record.id] for record in record_rows]
    assert ordered_values[0][text_field.id].text_value == "hello"
    assert ordered_values[0][integer_field.id].integer_value == 42
    assert ordered_values[0][decimal_field.id].decimal_value == Decimal("1.25000000")
    assert ordered_values[0][boolean_field.id].boolean_value is True
    assert ordered_values[0][enum_field.id].enum_option_id == enum_options[1].id
    assert ordered_values[1][text_field.id].text_value == ""
    assert ordered_values[1][integer_field.id].integer_value == 0
    assert ordered_values[1][decimal_field.id].decimal_value == Decimal("0.00000000")
    assert ordered_values[1][boolean_field.id].boolean_value is False
    assert ordered_values[1][enum_field.id].enum_option_id == enum_options[0].id
    for typed_values in ordered_values:
        assert typed_values[date_field.id].date_value == BUSINESS_DATE
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
        session,
        job.id,
        {BUSINESS_DATE},
        worker_id="test-worker",
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
    expected_dates = {BUSINESS_DATE, SECOND_DATE}
    staged = await data_import_service.start_staged_import(
        session,
        job.id,
        expected_dates,
        worker_id="test-worker",
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
            worker_id="test-worker",
        )

    await data_import_service.activate_import(
        session,
        import_id=job.id,
        worker_id="test-worker",
        expected_dates=expected_dates,
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
    first = await data_import_service.start_staged_import(
        session,
        job.id,
        {BUSINESS_DATE},
        worker_id="test-worker",
    )
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
        worker_id="test-worker",
    )
    first_record_ids = list(
        (
            await session.scalars(
                sa.select(DataRecord.id).where(DataRecord.snapshot_id == first_id)
            )
        ).all()
    )
    job.status = DataImportStatus.VALIDATING
    await session.flush()

    second = await data_import_service.start_staged_import(
        session,
        job.id,
        {BUSINESS_DATE},
        worker_id="test-worker",
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

    await data_import_service.discard_staged_import(
        session,
        staged_job.id,
        worker_id="test-worker",
        attempt_count=1,
    )

    assert await session.get(DataSnapshot, staged.id) is None
    assert await session.get(DataRecord, staged_record.id) is None
    assert await session.get(DataSnapshot, active.id) is not None
    assert await session.get(DataSnapshot, superseded.id) is not None
    assert await session.get(DataRecord, active_record.id) is not None
    assert await session.get(DataRecord, superseded_record.id) is not None


async def test_discard_rejects_stale_owner_without_deleting_current_staging(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        worker_id="current-worker",
        attempt_count=3,
    )
    staged = (
        await data_import_service.start_staged_import(
            session,
            job.id,
            {BUSINESS_DATE},
            worker_id="current-worker",
        )
    )[BUSINESS_DATE]
    record = await _add_record(
        session,
        snapshot=staged,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    staged_id = staged.id
    record_id = record.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.discard_staged_import(
            session,
            job.id,
            worker_id="stale-worker",
            attempt_count=3,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, staged_id) is not None
    assert await session.get(DataRecord, record_id) is not None


async def test_discard_rejects_expired_lease_without_deletion(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        expired=True,
        attempt_count=1,
    )
    staged = await _add_snapshot(
        session,
        table=table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    record = await _add_record(
        session,
        snapshot=staged,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    staged_id = staged.id
    record_id = record.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.discard_staged_import(
            session,
            job.id,
            worker_id="test-worker",
            attempt_count=1,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, staged_id) is not None
    assert await session.get(DataRecord, record_id) is not None


async def test_discard_rejects_stale_attempt_after_reclaim_without_deletion(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        worker_id="fresh-worker",
        attempt_count=2,
    )
    staged = (
        await data_import_service.start_staged_import(
            session,
            job.id,
            {BUSINESS_DATE},
            worker_id="fresh-worker",
        )
    )[BUSINESS_DATE]
    record = await _add_record(
        session,
        snapshot=staged,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    staged_id = staged.id
    record_id = record.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.discard_staged_import(
            session,
            job.id,
            worker_id="fresh-worker",
            attempt_count=1,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, staged_id) is not None
    assert await session.get(DataRecord, record_id) is not None


async def test_discard_rejects_wrong_status_without_deletion(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
        attempt_count=1,
    )
    job.lease_owner = "test-worker"
    job.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    await session.flush()
    staged = await _add_snapshot(
        session,
        table=table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    record = await _add_record(
        session,
        snapshot=staged,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    staged_id = staged.id
    record_id = record.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.discard_staged_import(
            session,
            job.id,
            worker_id="test-worker",
            attempt_count=1,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, staged_id) is not None
    assert await session.get(DataRecord, record_id) is not None


async def test_discard_allows_failed_job_with_retained_owner_and_attempt(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.FAILED,
        attempt_count=4,
    )
    job.lease_owner = "test-worker"
    await session.flush()
    staged = await _add_snapshot(
        session,
        table=table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    record = await _add_record(
        session,
        snapshot=staged,
        customer_id=customer.id,
        customer_name=customer.name,
    )
    staged_id = staged.id
    record_id = record.id

    await data_import_service.discard_staged_import(
        session,
        job.id,
        worker_id="test-worker",
        attempt_count=4,
    )

    assert await session.get(DataSnapshot, staged_id) is None
    assert await session.get(DataRecord, record_id) is None
    refreshed_job = await session.get(DataImportJob, job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == DataImportStatus.FAILED
    assert refreshed_job.lease_owner == "test-worker"
    assert refreshed_job.attempt_count == 4


async def test_discard_with_no_staged_rows_is_safe_and_idempotent(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.APPLYING,
        attempt_count=2,
    )

    await data_import_service.discard_staged_import(
        session,
        job.id,
        worker_id="test-worker",
        attempt_count=2,
    )
    await data_import_service.discard_staged_import(
        session,
        job.id,
        worker_id="test-worker",
        attempt_count=2,
    )

    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataSnapshot)
            .where(DataSnapshot.import_id == job.id)
        )
        == 0
    )


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
    expected_dates = {BUSINESS_DATE, SECOND_DATE}
    staged = await data_import_service.start_staged_import(
        session,
        job.id,
        expected_dates,
        worker_id="test-worker",
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
            expected_dates=expected_dates,
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
    await data_import_service.start_staged_import(
        session,
        job.id,
        {BUSINESS_DATE},
        worker_id="test-worker",
    )
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
            expected_dates={BUSINESS_DATE},
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
        await data_import_service.start_staged_import(
            session,
            job.id,
            {BUSINESS_DATE},
            worker_id="test-worker",
        )
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
            worker_id="test-worker",
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID


@pytest.mark.parametrize(
    "invalid_dates",
    [
        (),
        (BUSINESS_DATE, BUSINESS_DATE),
    ],
)
async def test_start_rejects_empty_or_duplicate_dates_before_cleanup(
    session: AsyncSession,
    invalid_dates: tuple[date, ...],
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(session, table=table, owner=owner)
    existing = (
        await data_import_service.start_staged_import(
            session,
            job.id,
            {BUSINESS_DATE},
            worker_id="test-worker",
        )
    )[BUSINESS_DATE]
    job.status = DataImportStatus.VALIDATING
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await data_import_service.start_staged_import(
            session,
            job.id,
            invalid_dates,
            worker_id="test-worker",
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, existing.id) is not None
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataSnapshot)
            .where(DataSnapshot.import_id == job.id)
        )
        == 1
    )


@pytest.mark.parametrize(
    ("job_status", "worker_id", "expired"),
    [
        (DataImportStatus.APPLYING, "test-worker", False),
        (DataImportStatus.VALIDATING, "other-worker", False),
        (DataImportStatus.VALIDATING, "test-worker", True),
    ],
)
async def test_start_rejects_wrong_status_owner_or_expired_lease_without_cleanup(
    session: AsyncSession,
    job_status: DataImportStatus,
    worker_id: str,
    expired: bool,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=job_status,
        expired=expired,
    )
    existing = await _add_snapshot(
        session,
        table=table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    existing_id = existing.id
    job_id = job.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.start_staged_import(
            session,
            job_id,
            {SECOND_DATE},
            worker_id=worker_id,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, existing_id) is not None
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataSnapshot)
            .where(DataSnapshot.import_id == job_id)
        )
        == 1
    )


@pytest.mark.parametrize(
    ("job_status", "worker_id", "expired"),
    [
        (DataImportStatus.VALIDATING, "test-worker", False),
        (DataImportStatus.APPLYING, "other-worker", False),
        (DataImportStatus.APPLYING, "test-worker", True),
    ],
)
async def test_append_rejects_wrong_status_owner_or_expired_lease_without_writes(
    session: AsyncSession,
    job_status: DataImportStatus,
    worker_id: str,
    expired: bool,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(session, table=table, owner=owner)
    snapshot = (
        await data_import_service.start_staged_import(
            session,
            job.id,
            {BUSINESS_DATE},
            worker_id="test-worker",
        )
    )[BUSINESS_DATE]
    job.status = job_status
    if expired:
        job.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await data_import_service.append_staged_rows(
            session,
            snapshot.id,
            [
                _validated_row(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    business_date=BUSINESS_DATE,
                    custom_values={},
                )
            ],
            worker_id=worker_id,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert await session.get(DataSnapshot, snapshot.id) is not None
    assert snapshot.row_count == 0
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataRecord)
            .where(DataRecord.snapshot_id == snapshot.id)
        )
        == 0
    )


async def test_append_rejects_snapshot_owned_by_another_import(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    first_table = await _make_table(session, owner)
    second_table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    first_job = await _make_job(session, table=first_table, owner=owner)
    second_job = await _make_job(
        session,
        table=second_table,
        owner=owner,
        worker_id="second-worker",
    )
    await data_import_service.start_staged_import(
        session,
        first_job.id,
        {BUSINESS_DATE},
        worker_id="test-worker",
    )
    second_snapshot = (
        await data_import_service.start_staged_import(
            session,
            second_job.id,
            {BUSINESS_DATE},
            worker_id="second-worker",
        )
    )[BUSINESS_DATE]

    with pytest.raises(BusinessError) as error:
        await data_import_service.append_staged_rows(
            session,
            second_snapshot.id,
            [
                _validated_row(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    business_date=BUSINESS_DATE,
                    custom_values={},
                )
            ],
            worker_id="test-worker",
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataRecord)
            .where(DataRecord.snapshot_id == second_snapshot.id)
        )
        == 0
    )


async def test_append_rejects_snapshot_table_mismatch_with_parent_job(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    parent_table = await _make_table(session, owner)
    foreign_table = await _make_table(session, owner)
    customer = await make_customer_row(session, owner=owner)
    job = await _make_job(
        session,
        table=parent_table,
        owner=owner,
        status=DataImportStatus.APPLYING,
    )
    foreign_snapshot = await _add_snapshot(
        session,
        table=foreign_table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )

    with pytest.raises(BusinessError) as error:
        await data_import_service.append_staged_rows(
            session,
            foreign_snapshot.id,
            [
                _validated_row(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    business_date=BUSINESS_DATE,
                    custom_values={},
                )
            ],
            worker_id="test-worker",
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataRecord)
            .where(DataRecord.snapshot_id == foreign_snapshot.id)
        )
        == 0
    )


async def test_activation_rejects_non_applying_job(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.VALIDATING,
    )
    staged = await _add_snapshot(
        session,
        table=table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    staged_id = staged.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.activate_import(
            session,
            import_id=job.id,
            worker_id="test-worker",
            expected_dates={BUSINESS_DATE},
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    refreshed = await session.get(DataSnapshot, staged_id)
    assert refreshed is not None
    assert refreshed.status == DataSnapshotStatus.STAGED


@pytest.mark.parametrize(
    ("staged_dates", "expected_dates"),
    [
        ({BUSINESS_DATE}, {BUSINESS_DATE, SECOND_DATE}),
        ({BUSINESS_DATE, SECOND_DATE}, {BUSINESS_DATE}),
    ],
)
async def test_activation_rejects_missing_or_extra_staged_dates(
    session: AsyncSession,
    staged_dates: set[date],
    expected_dates: set[date],
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(session, table=table, owner=owner)
    job_id = job.id
    table_id = table.id
    staged = await data_import_service.start_staged_import(
        session,
        job_id,
        staged_dates,
        worker_id="test-worker",
    )
    staged_ids = {
        business_date: snapshot.id for business_date, snapshot in staged.items()
    }

    with pytest.raises(BusinessError) as error:
        await data_import_service.activate_import(
            session,
            import_id=job_id,
            worker_id="test-worker",
            expected_dates=expected_dates,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    for snapshot_id in staged_ids.values():
        refreshed = await session.get(DataSnapshot, snapshot_id)
        assert refreshed is not None
        assert refreshed.status == DataSnapshotStatus.STAGED
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataCurrentSnapshot)
            .where(DataCurrentSnapshot.c.table_id == table_id)
        )
        == 0
    )


async def test_activation_rejects_foreign_table_snapshot(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    foreign_table = await _make_table(session, owner)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.APPLYING,
    )
    foreign_snapshot = await _add_snapshot(
        session,
        table=foreign_table,
        job=job,
        business_date=BUSINESS_DATE,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    foreign_snapshot_id = foreign_snapshot.id
    table_id = table.id
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await data_import_service.activate_import(
            session,
            import_id=job.id,
            worker_id="test-worker",
            expected_dates={BUSINESS_DATE},
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    refreshed = await session.get(DataSnapshot, foreign_snapshot_id)
    assert refreshed is not None
    assert refreshed.status == DataSnapshotStatus.STAGED
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(DataCurrentSnapshot)
            .where(DataCurrentSnapshot.c.table_id == table_id)
        )
        == 0
    )


@pytest.mark.parametrize(
    "expected_dates",
    [
        (),
        (BUSINESS_DATE, BUSINESS_DATE),
    ],
)
async def test_activation_rejects_empty_or_duplicate_expected_dates_without_mutation(
    session: AsyncSession,
    expected_dates: tuple[date, ...],
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await _make_job(session, table=table, owner=owner)
    snapshot = (
        await data_import_service.start_staged_import(
            session,
            job.id,
            {BUSINESS_DATE},
            worker_id="test-worker",
        )
    )[BUSINESS_DATE]

    with pytest.raises(BusinessError) as error:
        await data_import_service.activate_import(
            session,
            import_id=job.id,
            worker_id="test-worker",
            expected_dates=expected_dates,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    refreshed = await session.get(DataSnapshot, snapshot.id)
    assert refreshed is not None
    assert refreshed.status == DataSnapshotStatus.STAGED


async def test_start_waits_for_direct_table_row_lock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    table_id: int | None = None
    user_id: int | None = None
    try:
        async with factory() as setup:
            owner = await make_user(setup)
            table = await _make_table(setup, owner)
            job = await _make_job(setup, table=table, owner=owner)
            table_id = table.id
            user_id = owner.id
            job_id = job.id
            await setup.commit()

        async with factory() as holder:
            locked = await holder.scalar(
                sa.select(DataTableDefinition)
                .where(DataTableDefinition.id == table_id)
                .with_for_update()
            )
            assert locked is not None
            async with factory() as contender:
                task = asyncio.create_task(
                    data_import_service.start_staged_import(
                        contender,
                        job_id,
                        {BUSINESS_DATE},
                        worker_id="test-worker",
                    )
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
                await holder.rollback()
                snapshots = await asyncio.wait_for(task, timeout=5)

        assert set(snapshots) == {BUSINESS_DATE}
        assert snapshots[BUSINESS_DATE].status == DataSnapshotStatus.STAGED
    finally:
        if table_id is not None and user_id is not None:
            await _delete_committed_table(
                factory,
                table_id=table_id,
                user_id=user_id,
            )
