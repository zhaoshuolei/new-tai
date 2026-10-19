"""Import job, lease, and snapshot pointer persistence.

The lock order in this module is fixed: ``custom_data_table`` first, import
rows second, and snapshot/current-pointer rows last.  Claim uses MySQL
``SKIP LOCKED`` after taking the table lock so multiple workers can make
progress without reversing that order.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import (
    DataCurrentSnapshot,
    DataImportError,
    DataImportJob,
    DataImportSource,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
)

_UNFINISHED_IMPORT_STATUSES: tuple[DataImportStatus, ...] = (
    DataImportStatus.QUEUED,
    DataImportStatus.VALIDATING,
    DataImportStatus.APPLYING,
)
_CLAIMABLE_LEASED_STATUSES: tuple[DataImportStatus, ...] = (
    DataImportStatus.VALIDATING,
    DataImportStatus.APPLYING,
)


def _utc_now() -> datetime:
    """Return the naive UTC representation used by MySQL ``DATETIME``."""
    return datetime.now(UTC).replace(tzinfo=None)


def _import_in_progress_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_IN_PROGRESS,
        "Data table has an unfinished import",
        http_status=409,
    )


def _table_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_NOT_FOUND,
        "Data table not found",
        http_status=404,
    )


def _claimable_condition(now: datetime) -> ColumnElement[bool]:
    """SQL condition for a queued job or an active-status job with stale lease."""
    return or_(
        DataImportJob.status == DataImportStatus.QUEUED,
        and_(
            DataImportJob.status.in_(_CLAIMABLE_LEASED_STATUSES),
            or_(
                DataImportJob.lease_expires_at.is_(None),
                DataImportJob.lease_expires_at <= now,
            ),
        ),
    )


async def create_import_job(
    session: AsyncSession,
    *,
    table_id: int,
    source: DataImportSource,
    storage_path: str,
    content_sha256: str,
    size_bytes: int,
    requested_by: int,
    original_filename: str | None = None,
) -> DataImportJob:
    """Create one QUEUED job while holding the table lock.

    The caller owns the transaction boundary.  Enqueue services must commit
    after this function returns so the table lock serializes concurrent
    requests across processes.
    """
    table = await session.scalar(
        select(DataTableDefinition)
        .where(DataTableDefinition.id == table_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if table is None:
        raise _table_not_found_error()
    if await has_unfinished_import(session, table_id):
        raise _import_in_progress_error()

    job = DataImportJob(
        table_id=table_id,
        source=source,
        status=DataImportStatus.QUEUED,
        original_filename=original_filename,
        storage_path=storage_path,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        requested_by=requested_by,
    )
    session.add(job)
    await session.flush()
    return job


async def get_import_job(
    session: AsyncSession,
    import_id: int,
) -> DataImportJob | None:
    return await session.get(DataImportJob, import_id)


async def list_import_jobs(
    session: AsyncSession,
    table_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
) -> list[DataImportJob]:
    statement = (
        select(DataImportJob)
        .where(DataImportJob.table_id == table_id)
        .order_by(DataImportJob.created_at.desc(), DataImportJob.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


async def list_import_errors(
    session: AsyncSession,
    import_id: int,
    *,
    offset: int = 0,
    limit: int = 100,
) -> list[DataImportError]:
    statement = (
        select(DataImportError)
        .where(DataImportError.import_id == import_id)
        .order_by(DataImportError.id)
        .offset(offset)
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


async def list_current_snapshots(
    session: AsyncSession,
    table_id: int,
) -> list[DataSnapshot]:
    """Read exactly the snapshots selected by the current pointers."""
    statement = (
        select(DataSnapshot)
        .join(
            DataCurrentSnapshot,
            DataCurrentSnapshot.c.snapshot_id == DataSnapshot.id,
        )
        .where(
            DataCurrentSnapshot.c.table_id == table_id,
            DataSnapshot.table_id == table_id,
        )
        .order_by(DataSnapshot.business_date)
    )
    return list((await session.scalars(statement)).all())


async def has_unfinished_import(session: AsyncSession, table_id: int) -> bool:
    """Current-read an unfinished import; callers must hold the table lock."""
    statement = (
        select(DataImportJob.id)
        .where(
            DataImportJob.table_id == table_id,
            DataImportJob.status.in_(_UNFINISHED_IMPORT_STATUSES),
        )
        .limit(1)
        .with_for_update()
    )
    return await session.scalar(statement) is not None


async def _delete_staged_rows_for_import(
    session: AsyncSession,
    import_id: int,
) -> None:
    """Delete only STAGED snapshot data for one import, in FK-safe order."""
    snapshot_ids = list(
        (
            await session.scalars(
                select(DataSnapshot.id)
                .where(
                    DataSnapshot.import_id == import_id,
                    DataSnapshot.status == DataSnapshotStatus.STAGED,
                )
                .with_for_update()
            )
        ).all()
    )
    if not snapshot_ids:
        return

    record_ids = list(
        (
            await session.scalars(
                select(DataRecord.id)
                .where(DataRecord.snapshot_id.in_(snapshot_ids))
                .with_for_update()
            )
        ).all()
    )
    if record_ids:
        await session.execute(
            delete(DataRecordValue).where(DataRecordValue.record_id.in_(record_ids))
        )
        await session.execute(delete(DataRecord).where(DataRecord.id.in_(record_ids)))
    await session.execute(
        DataCurrentSnapshot.delete().where(
            DataCurrentSnapshot.c.snapshot_id.in_(snapshot_ids)
        )
    )
    await session.execute(delete(DataSnapshot).where(DataSnapshot.id.in_(snapshot_ids)))


async def claim_next_import_job(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> DataImportJob | None:
    """Claim one eligible job and commit the durable lease before parsing.

    Candidate discovery is a non-locking snapshot read.  Both the table and
    import row are then current-read with ``SKIP LOCKED`` in the mandated
    order.  If another worker holds either row, this worker moves to another
    candidate rather than blocking.
    """
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = _utc_now()
    candidate_table_ids = list(
        (
            await session.scalars(
                select(DataImportJob.table_id)
                .where(_claimable_condition(now))
                .order_by(DataImportJob.queued_at, DataImportJob.id)
                .limit(100)
            )
        ).all()
    )
    if not candidate_table_ids:
        await session.commit()
        return None

    for table_id in dict.fromkeys(candidate_table_ids):
        table = await session.scalar(
            select(DataTableDefinition)
            .where(DataTableDefinition.id == table_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if table is None:
            continue

        job = await session.scalar(
            select(DataImportJob)
            .where(
                DataImportJob.table_id == table_id,
                _claimable_condition(now),
            )
            .order_by(DataImportJob.queued_at, DataImportJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if job is None:
            continue

        await _delete_staged_rows_for_import(session, job.id)
        await session.execute(
            delete(DataImportError).where(DataImportError.import_id == job.id)
        )
        job.status = DataImportStatus.VALIDATING
        job.attempt_count += 1
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.heartbeat_at = now
        job.started_at = job.started_at or now
        job.finished_at = None
        job.failure_code = None
        job.failure_message = None
        job.total_rows = 0
        job.valid_rows = 0
        job.invalid_rows = 0
        job.date_from = None
        job.date_to = None
        job.error_count = 0
        job.progress_percent = 0
        await session.flush()
        await session.commit()
        return job

    await session.commit()
    return None


async def renew_import_lease(
    session: AsyncSession,
    *,
    import_id: int,
    worker_id: str,
    lease_seconds: int,
    attempt_count: int | None = None,
) -> bool:
    """Extend a still-live lease owned by ``worker_id``.

    An expired lease cannot be revived: another worker may already have
    claimed the job using ``SKIP LOCKED``. When supplied, ``attempt_count``
    also prevents an abandoned process with a reused worker ID from renewing a
    later attempt.
    """
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = _utc_now()
    conditions = [
        DataImportJob.id == import_id,
        DataImportJob.lease_owner == worker_id,
        DataImportJob.status.in_(_CLAIMABLE_LEASED_STATUSES),
        DataImportJob.lease_expires_at.is_not(None),
        DataImportJob.lease_expires_at > now,
    ]
    if attempt_count is not None:
        conditions.append(DataImportJob.attempt_count == attempt_count)
    result = await session.execute(
        update(DataImportJob)
        .where(*conditions)
        .values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            heartbeat_at=now,
        )
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def get_import_job_for_update(
    session: AsyncSession,
    import_id: int,
) -> DataImportJob | None:
    """Lock an import row after its table has already been locked."""
    return await session.scalar(
        select(DataImportJob)
        .where(DataImportJob.id == import_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def discard_staged_rows(
    session: AsyncSession,
    import_id: int,
) -> None:
    """Repository-level staged cleanup without committing."""
    await _delete_staged_rows_for_import(session, import_id)


__all__: tuple[str, ...] = (
    "claim_next_import_job",
    "create_import_job",
    "discard_staged_rows",
    "get_import_job",
    "get_import_job_for_update",
    "has_unfinished_import",
    "list_current_snapshots",
    "list_import_errors",
    "list_import_jobs",
    "renew_import_lease",
)
