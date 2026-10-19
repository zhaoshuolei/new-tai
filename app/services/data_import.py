"""Snapshot persistence for validated imports.

Staging is durable but invisible: records are attached only to ``STAGED``
snapshots.  Activation is the one short transaction that makes a complete
multi-date batch visible by switching every current pointer together.
"""

from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import (
    DataCurrentSnapshot,
    DataFieldDefinition,
    DataImportJob,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
    DataTableStatus,
)
from app.repositories import data_import as data_import_repository
from app.services.data_import_parser import ValidatedImportRow

_ACTIVE_LEASE_STATUSES: frozenset[DataImportStatus] = frozenset(
    {
        DataImportStatus.VALIDATING,
        DataImportStatus.APPLYING,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_NOT_FOUND,
        "Import job not found",
        http_status=404,
    )


def _invalid_import_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_INVALID,
        message,
        http_status=422,
    )


def _require_live_lease(job: DataImportJob, worker_id: str) -> None:
    if job.status not in _ACTIVE_LEASE_STATUSES:
        raise _invalid_import_error("Import job is not active")
    if job.lease_owner != worker_id:
        raise _invalid_import_error("Import lease is owned by another worker")
    if job.lease_expires_at is None or job.lease_expires_at <= _utc_now():
        raise _invalid_import_error("Import lease has expired")


async def _lock_job_and_table(
    session: AsyncSession,
    import_id: int,
) -> DataImportJob:
    """Lock table then import, preserving the binding lock order."""
    table_id = await session.scalar(
        select(DataImportJob.table_id).where(DataImportJob.id == import_id)
    )
    if table_id is None:
        raise _not_found_error()
    table = await session.scalar(
        select(DataTableDefinition)
        .where(DataTableDefinition.id == table_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if table is None:
        raise _not_found_error()
    job = await data_import_repository.get_import_job_for_update(session, import_id)
    if job is None:
        raise _not_found_error()
    return job


async def start_staged_import(
    session: AsyncSession,
    job: DataImportJob,
    business_dates: Collection[date],
) -> dict[date, DataSnapshot]:
    """Allocate one STAGED snapshot per date and enter APPLYING.

    The table lock serializes version allocation.  Any STAGED data left by a
    previous attempt of this same job is removed first; successful history is
    never touched.
    """
    table = await session.scalar(
        select(DataTableDefinition)
        .where(DataTableDefinition.id == job.table_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if table is None:
        raise _not_found_error()
    locked_job = await data_import_repository.get_import_job_for_update(session, job.id)
    if locked_job is None:
        raise _not_found_error()
    if locked_job.status not in _ACTIVE_LEASE_STATUSES:
        raise _invalid_import_error("Import job is not active")
    if locked_job.lease_expires_at is None or locked_job.lease_expires_at <= _utc_now():
        raise _invalid_import_error("Import lease has expired")

    await data_import_repository.discard_staged_rows(session, locked_job.id)
    dates = sorted(set(business_dates))
    snapshots: dict[date, DataSnapshot] = {}
    if dates:
        maximum_by_date = {
            business_date: maximum
            for business_date, maximum in (
                await session.execute(
                    select(
                        DataSnapshot.business_date,
                        func.max(DataSnapshot.version),
                    )
                    .where(
                        DataSnapshot.table_id == table.id,
                        DataSnapshot.business_date.in_(dates),
                    )
                    .group_by(DataSnapshot.business_date)
                )
            ).all()
        }
        for business_date in dates:
            snapshot = DataSnapshot(
                table_id=table.id,
                business_date=business_date,
                import_id=locked_job.id,
                version=int(maximum_by_date.get(business_date) or 0) + 1,
                status=DataSnapshotStatus.STAGED,
                row_count=0,
            )
            session.add(snapshot)
            snapshots[business_date] = snapshot
        locked_job.status = DataImportStatus.APPLYING
        await session.flush()
    await session.commit()
    return snapshots


async def append_staged_rows(
    session: AsyncSession,
    snapshot_id: int,
    rows: Sequence[ValidatedImportRow],
    *,
    batch_size: int = 1000,
) -> None:
    """Insert typed records and values in bounded ORM batches.

    Rows remain invisible because the destination snapshot is locked and must
    still be ``STAGED``.  The caller owns the final commit.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    snapshot = await session.scalar(
        select(DataSnapshot)
        .where(DataSnapshot.id == snapshot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if snapshot is None:
        raise _invalid_import_error("Import snapshot not found")
    if snapshot.status != DataSnapshotStatus.STAGED:
        raise _invalid_import_error("Only STAGED snapshots can receive rows")

    field_ids = {field_id for row in rows for field_id in row.custom_values}
    if field_ids:
        existing_field_ids = set(
            (
                await session.scalars(
                    select(DataFieldDefinition.id).where(
                        DataFieldDefinition.table_id == snapshot.table_id,
                        DataFieldDefinition.id.in_(field_ids),
                    )
                )
            ).all()
        )
        if existing_field_ids != field_ids:
            raise _invalid_import_error(
                "A custom field does not belong to the snapshot table"
            )
        for row in rows:
            for field_id, value in row.custom_values.items():
                if value.field_id != field_id:
                    raise _invalid_import_error(
                        "Custom field value key does not match its field ID"
                    )

    if any(row.business_date != snapshot.business_date for row in rows):
        raise _invalid_import_error(
            "Import row business date does not match the snapshot"
        )

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        if not batch:
            continue
        records = [
            DataRecord(
                snapshot_id=snapshot.id,
                table_id=snapshot.table_id,
                business_date=snapshot.business_date,
                upstream_customer_id=row.upstream_customer_id,
                downstream_customer_id=row.downstream_customer_id,
                upstream_customer_name_source=row.upstream_customer_name_source,
                downstream_customer_name_source=row.downstream_customer_name_source,
                revenue=row.revenue,
                cost=row.cost,
            )
            for row in batch
        ]
        session.add_all(records)
        await session.flush()
        values = [
            DataRecordValue(
                record_id=record.id,
                field_id=field_value.field_id,
                text_value=field_value.text_value,
                integer_value=field_value.integer_value,
                decimal_value=field_value.decimal_value,
                date_value=field_value.date_value,
                boolean_value=field_value.boolean_value,
                enum_option_id=field_value.enum_option_id,
            )
            for record, row in zip(records, batch, strict=True)
            for field_value in row.field_values
        ]
        if values:
            session.add_all(values)
            await session.flush()
        snapshot.row_count += len(batch)
        await session.flush()


async def _upsert_current_pointer(
    session: AsyncSession,
    *,
    table_id: int,
    business_date: date,
    snapshot_id: int,
) -> None:
    statement = insert(DataCurrentSnapshot).values(
        table_id=table_id,
        business_date=business_date,
        snapshot_id=snapshot_id,
    )
    statement = statement.on_duplicate_key_update(
        snapshot_id=statement.inserted.snapshot_id,
        updated_at=func.utc_timestamp(6),
    )
    await session.execute(statement)


async def activate_import(
    session: AsyncSession,
    *,
    import_id: int,
    worker_id: str,
) -> None:
    """Atomically activate every staged date for one complete import.

    Old ACTIVE rows are cleared before new ACTIVE rows in the same flush so
    the database's active-only unique key is never transiently violated.
    Any exception rolls the whole session transaction back.
    """
    try:
        job = await _lock_job_and_table(session, import_id)
        _require_live_lease(job, worker_id)
        staged = list(
            (
                await session.scalars(
                    select(DataSnapshot)
                    .where(
                        DataSnapshot.import_id == import_id,
                        DataSnapshot.status == DataSnapshotStatus.STAGED,
                    )
                    .order_by(DataSnapshot.business_date, DataSnapshot.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if not staged:
            raise _invalid_import_error("Import has no STAGED snapshots")
        staged_dates = [item.business_date for item in staged]
        if len(staged_dates) != len(set(staged_dates)):
            raise _invalid_import_error(
                "Import has multiple STAGED snapshots for one business date"
            )

        old_active = list(
            (
                await session.scalars(
                    select(DataSnapshot)
                    .where(
                        DataSnapshot.table_id == job.table_id,
                        DataSnapshot.business_date.in_(staged_dates),
                        DataSnapshot.status == DataSnapshotStatus.ACTIVE,
                    )
                    .order_by(DataSnapshot.business_date, DataSnapshot.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        await session.execute(
            select(DataCurrentSnapshot)
            .where(
                DataCurrentSnapshot.c.table_id == job.table_id,
                DataCurrentSnapshot.c.business_date.in_(staged_dates),
            )
            .with_for_update()
        )

        now = _utc_now()
        for snapshot in old_active:
            snapshot.status = DataSnapshotStatus.SUPERSEDED
        if old_active:
            await session.flush()

        for snapshot in staged:
            snapshot.status = DataSnapshotStatus.ACTIVE
            snapshot.activated_at = now
        await session.flush()

        for snapshot in staged:
            await _upsert_current_pointer(
                session,
                table_id=job.table_id,
                business_date=snapshot.business_date,
                snapshot_id=snapshot.id,
            )

        table = await session.get(DataTableDefinition, job.table_id)
        if table is None:
            raise _not_found_error()
        if table.status != DataTableStatus.LOCKED or table.locked_at is None:
            table.status = DataTableStatus.LOCKED
            table.locked_at = now
        job.status = DataImportStatus.SUCCEEDED
        job.progress_percent = 100
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def discard_staged_import(
    session: AsyncSession,
    import_id: int,
) -> None:
    """Delete only STAGED data owned by this import and commit."""
    try:
        await _lock_job_and_table(session, import_id)
        await data_import_repository.discard_staged_rows(session, import_id)
        await session.commit()
    except Exception:
        await session.rollback()
        raise


__all__: tuple[str, ...] = (
    "activate_import",
    "append_staged_rows",
    "discard_staged_import",
    "start_staged_import",
)
