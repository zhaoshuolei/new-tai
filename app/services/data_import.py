"""Snapshot persistence for validated imports.

Staging is durable but invisible: records are attached only to ``STAGED``
snapshots.  Activation is the one short transaction that makes a complete
multi-date batch visible by switching every current pointer together.
"""

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import Collection, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.db.session import session_factory
from app.models.audit_log import AuditResult
from app.models.contact import ContactStatus
from app.models.customer import Customer
from app.models.data_management import (
    DataCurrentSnapshot,
    DataFieldDefinition,
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
from app.models.user import UserStatus
from app.repositories import data_import as data_import_repository
from app.repositories import data_table as data_table_repository
from app.repositories import user as user_repository
from app.repositories.data_table import ResolvedDataDefinition
from app.services import data_import_parser
from app.services.audit import actor_from_context, audited, record_audit_entry
from app.services.auth import build_auth_context
from app.services.data_import_parser import (
    CustomerIdentity,
    ParsedImportRow,
    RowIssue,
    ValidatedImportRow,
)
from app.services.data_import_storage import StoredImportSource, read_import_source
from app.services.data_scope import apply_customer_scope

logger = logging.getLogger(__name__)

_MAX_ERROR_DETAILS = 10_000
_STAGE_BATCH_SIZE = 1_000
_SOURCE_COPY_CHUNK_SIZE = 64 * 1024
_LEASE_SECONDS = 300
_LEASE_RENEW_INTERVAL_SECONDS = 60.0


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


def _require_import_permission(context: AuthContext) -> None:
    if "data:import" not in context.permission_codes:
        raise BusinessError(
            ErrorCode.FORBIDDEN,
            "Import permission is required",
            http_status=403,
        )


@dataclass(frozen=True)
class _ClaimIdentity:
    job_id: int
    table_id: int
    requested_by: int
    attempt_count: int
    worker_id: str
    storage_path: str
    source: DataImportSource
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class _ValidationSummary:
    total_rows: int
    valid_rows: int
    invalid_rows: int
    error_count: int
    business_dates: frozenset[date]
    issues: tuple[RowIssue, ...]


class _ImportFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _LeaseLost(Exception):
    """The replacement heartbeat could no longer prove ownership."""


def _claim_identity(job: DataImportJob, worker_id: str) -> _ClaimIdentity:
    return _ClaimIdentity(
        job_id=job.id,
        table_id=job.table_id,
        requested_by=job.requested_by,
        attempt_count=job.attempt_count,
        worker_id=worker_id,
        storage_path=job.storage_path,
        source=job.source,
        content_sha256=job.content_sha256,
        size_bytes=job.size_bytes,
    )


def _require_claim_identity(
    job: DataImportJob,
    identity: _ClaimIdentity,
    *,
    expected_statuses: Collection[DataImportStatus],
) -> None:
    """Reject a stale claim after the table and import rows are locked."""
    if (
        job.id != identity.job_id
        or job.table_id != identity.table_id
        or job.lease_owner != identity.worker_id
        or job.attempt_count != identity.attempt_count
        or job.status not in expected_statuses
        or job.lease_expires_at is None
        or job.lease_expires_at <= _utc_now()
    ):
        raise _LeaseLost


def _safe_failure(error: Exception) -> tuple[str, str]:
    if isinstance(error, _ImportFailure):
        return error.code, error.message
    if isinstance(error, BusinessError):
        try:
            code = ErrorCode(error.code).name
        except ValueError:
            code = "DATA_IMPORT_INVALID"
        return code, error.message
    return ErrorCode.INTERNAL_ERROR.name, "Data import processing failed"


@audited(
    action="data_import:request",
    resource_type="custom_data_import",
    resource_id_attr="id",
)
async def enqueue_import(
    session: AsyncSession,
    table_id: int,
    *,
    source: DataImportSource,
    stored: StoredImportSource,
    context: AuthContext,
) -> DataImportJob:
    """Persist a QUEUED import after serializing on the data table row.

    The private source must already have been written and hashed by Task 5.
    This function owns the transaction boundary and commits before returning.
    """
    _require_import_permission(context)
    try:
        job = await data_import_repository.create_import_job(
            session,
            table_id=table_id,
            source=source,
            storage_path=stored.storage_path,
            original_filename=stored.original_name,
            content_sha256=stored.content_sha256,
            size_bytes=stored.size_bytes,
            requested_by=context.user_id,
        )
        await session.commit()
        return job
    except Exception:
        await session.rollback()
        raise


def _validated_dates(
    values: Collection[date],
    *,
    name: str,
) -> tuple[date, ...]:
    dates = tuple(values)
    if not dates:
        raise _invalid_import_error(f"{name} must not be empty")
    if len(dates) != len(set(dates)):
        raise _invalid_import_error(f"{name} must not contain duplicates")
    return dates


def _require_live_lease(
    job: DataImportJob,
    worker_id: str,
    *,
    expected_status: DataImportStatus,
) -> None:
    if job.status != expected_status:
        raise _invalid_import_error(
            f"Import job must be {expected_status.value} for this operation"
        )
    if job.lease_owner != worker_id:
        raise _invalid_import_error("Import lease is owned by another worker")
    if job.lease_expires_at is None or job.lease_expires_at <= _utc_now():
        raise _invalid_import_error("Import lease has expired")


def _require_staged_cleanup_owner(
    job: DataImportJob,
    *,
    worker_id: str,
    attempt_count: int,
) -> None:
    """Allow cleanup only for the current attempt's retained owner."""
    if job.attempt_count != attempt_count:
        raise _invalid_import_error("Import attempt does not match")
    if job.lease_owner != worker_id:
        raise _invalid_import_error("Import lease is owned by another worker")
    if job.status == DataImportStatus.FAILED:
        return
    if job.status not in {
        DataImportStatus.VALIDATING,
        DataImportStatus.APPLYING,
    }:
        raise _invalid_import_error(
            "Import job must be VALIDATING, APPLYING, or FAILED for cleanup"
        )
    if job.lease_expires_at is None or job.lease_expires_at <= _utc_now():
        raise _invalid_import_error("Import lease has expired")


async def _lock_table_and_job(
    session: AsyncSession,
    import_id: int,
) -> tuple[DataTableDefinition, DataImportJob]:
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
    if job.table_id != table.id:
        raise _invalid_import_error("Import job table does not match its locked table")
    return table, job


async def _lock_claimed_job(
    session: AsyncSession,
    identity: _ClaimIdentity,
    *,
    expected_statuses: Collection[DataImportStatus],
) -> DataImportJob:
    """Lock table then import and prove the captured claim is still current."""
    _, job = await _lock_table_and_job(session, identity.job_id)
    _require_claim_identity(
        job,
        identity,
        expected_statuses=expected_statuses,
    )
    return job


async def _lock_snapshot_chain(
    session: AsyncSession,
    snapshot_id: int,
) -> tuple[DataTableDefinition, DataImportJob, DataSnapshot]:
    """Lock table then parent import then snapshot for a staging write."""
    reference = (
        await session.execute(
            select(DataSnapshot.table_id, DataSnapshot.import_id).where(
                DataSnapshot.id == snapshot_id
            )
        )
    ).one_or_none()
    if reference is None:
        raise _invalid_import_error("Import snapshot not found")

    table = await session.scalar(
        select(DataTableDefinition)
        .where(DataTableDefinition.id == reference.table_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if table is None:
        raise _invalid_import_error("Import snapshot table not found")
    job = await data_import_repository.get_import_job_for_update(
        session, reference.import_id
    )
    if job is None:
        raise _invalid_import_error("Import snapshot parent job not found")
    snapshot = await session.scalar(
        select(DataSnapshot)
        .where(DataSnapshot.id == snapshot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if snapshot is None:
        raise _invalid_import_error("Import snapshot not found")
    return table, job, snapshot


async def start_staged_import(
    session: AsyncSession,
    job_id: int,
    business_dates: Collection[date],
    *,
    worker_id: str,
) -> dict[date, DataSnapshot]:
    """Allocate one STAGED snapshot per date for the live validation owner.

    The table lock serializes version allocation. Any STAGED data left by a
    previous attempt of this same job is removed only after ownership, status,
    lease, and the complete date input have been validated.
    """
    dates = tuple(sorted(_validated_dates(business_dates, name="business_dates")))
    try:
        table, job = await _lock_table_and_job(session, job_id)
        _require_live_lease(
            job,
            worker_id,
            expected_status=DataImportStatus.VALIDATING,
        )

        prior_staged = list(
            (
                await session.scalars(
                    select(DataSnapshot)
                    .where(
                        DataSnapshot.import_id == job.id,
                        DataSnapshot.status == DataSnapshotStatus.STAGED,
                    )
                    .order_by(DataSnapshot.business_date, DataSnapshot.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if any(snapshot.table_id != table.id for snapshot in prior_staged):
            raise _invalid_import_error(
                "A STAGED snapshot does not belong to the import table"
            )

        await data_import_repository.discard_staged_rows(session, job.id)
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
        snapshots: dict[date, DataSnapshot] = {}
        for business_date in dates:
            snapshot = DataSnapshot(
                table_id=table.id,
                business_date=business_date,
                import_id=job.id,
                version=int(maximum_by_date.get(business_date) or 0) + 1,
                status=DataSnapshotStatus.STAGED,
                row_count=0,
            )
            session.add(snapshot)
            snapshots[business_date] = snapshot
        job.status = DataImportStatus.APPLYING
        await session.flush()
        await session.commit()
        return snapshots
    except Exception:
        await session.rollback()
        raise


async def append_staged_rows(
    session: AsyncSession,
    snapshot_id: int,
    rows: Sequence[ValidatedImportRow],
    *,
    worker_id: str,
    batch_size: int = 1000,
) -> None:
    """Insert typed records for the live APPLYING owner of the parent import.

    Rows remain invisible because the destination snapshot is locked and must
    still be ``STAGED``. The caller owns the final commit.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    table, job, snapshot = await _lock_snapshot_chain(session, snapshot_id)
    if (
        job.table_id != table.id
        or snapshot.table_id != job.table_id
        or snapshot.import_id != job.id
    ):
        raise _invalid_import_error(
            "Import snapshot does not belong to the locked job and table"
        )
    _require_live_lease(
        job,
        worker_id,
        expected_status=DataImportStatus.APPLYING,
    )
    if snapshot.status != DataSnapshotStatus.STAGED:
        raise _invalid_import_error("Only STAGED snapshots can receive rows")
    if any(row.business_date != snapshot.business_date for row in rows):
        raise _invalid_import_error(
            "Import row business date does not match the snapshot"
        )

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
    expected_dates: Collection[date],
) -> None:
    """Atomically activate exactly the expected dates for one live import.

    Old ACTIVE rows are cleared before new ACTIVE rows in the same flush so
    the database's active-only unique key is never transiently violated.
    Any exception rolls the whole session transaction back.
    """
    dates = _validated_dates(expected_dates, name="expected_dates")
    expected_date_set = set(dates)
    try:
        table, job = await _lock_table_and_job(session, import_id)
        _require_live_lease(
            job,
            worker_id,
            expected_status=DataImportStatus.APPLYING,
        )
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
        if any(snapshot.table_id != job.table_id for snapshot in staged):
            raise _invalid_import_error(
                "A STAGED snapshot does not belong to the import table"
            )
        staged_dates = [snapshot.business_date for snapshot in staged]
        if len(staged_dates) != len(set(staged_dates)):
            raise _invalid_import_error(
                "Import has multiple STAGED snapshots for one business date"
            )
        if set(staged_dates) != expected_date_set:
            raise _invalid_import_error(
                "STAGED snapshots do not match the expected business dates"
            )

        old_active = list(
            (
                await session.scalars(
                    select(DataSnapshot)
                    .where(
                        DataSnapshot.table_id == table.id,
                        DataSnapshot.business_date.in_(dates),
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
                DataCurrentSnapshot.c.table_id == table.id,
                DataCurrentSnapshot.c.business_date.in_(dates),
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
                table_id=table.id,
                business_date=snapshot.business_date,
                snapshot_id=snapshot.id,
            )

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
    *,
    worker_id: str,
    attempt_count: int,
) -> None:
    """Delete STAGED data owned by the current attempt and retained owner.

    The table and import rows are locked before cleanup. A stale worker or
    attempt, a terminal status without retained cleanup identity, and an
    expired live lease are rejected before any snapshot is selected.
    """
    try:
        _, job = await _lock_table_and_job(session, import_id)
        _require_staged_cleanup_owner(
            job,
            worker_id=worker_id,
            attempt_count=attempt_count,
        )
        await data_import_repository.discard_staged_rows(session, job.id)
        await session.commit()
    except Exception:
        await session.rollback()
        raise


def _source_filename(source: DataImportSource) -> str:
    return "source.xlsx" if source == DataImportSource.EXCEL else "source.json"


def _materialize_source(
    *,
    storage_path: str,
    target_path: Path,
    expected_sha256: str,
    expected_size: int,
) -> None:
    """Copy and verify a private source entirely off the event-loop thread."""
    digest = hashlib.sha256()
    copied = 0
    with read_import_source(storage_path) as source, target_path.open("wb") as target:
        while chunk := source.read(_SOURCE_COPY_CHUNK_SIZE):
            digest.update(chunk)
            target.write(chunk)
            copied += len(chunk)
    if copied != expected_size or digest.hexdigest() != expected_sha256:
        raise BusinessError(
            ErrorCode.DATA_IMPORT_INVALID,
            "Data import source metadata does not match",
            http_status=422,
        )


def _row_iterator(
    *,
    source: DataImportSource,
    source_path: Path,
    definition: ResolvedDataDefinition,
    customer_index: dict[str, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
) -> Iterator[ParsedImportRow]:
    if source == DataImportSource.EXCEL:
        return data_import_parser.iter_excel_rows(
            source_path,
            definition,
            customer_index,
            allowed_customer_ids,
        )
    return data_import_parser.iter_json_rows(
        source_path,
        definition,
        customer_index,
        allowed_customer_ids,
    )


def _take_rows(
    iterator: Iterator[ParsedImportRow],
    batch_size: int,
) -> tuple[list[ParsedImportRow], bool]:
    rows: list[ParsedImportRow] = []
    for _ in range(batch_size):
        try:
            rows.append(next(iterator))
        except StopIteration:
            return rows, True
    return rows, False


def _close_iterator(iterator: Iterator[ParsedImportRow]) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


def _raise_if_heartbeat_failed(task: asyncio.Task[None]) -> None:
    if task.done():
        task.result()


async def _renew_import_lease_once(identity: _ClaimIdentity) -> None:
    async with session_factory() as session:
        renewed = await data_import_repository.renew_import_lease(
            session,
            import_id=identity.job_id,
            worker_id=identity.worker_id,
            lease_seconds=_LEASE_SECONDS,
            attempt_count=identity.attempt_count,
        )
    if not renewed:
        raise _LeaseLost


async def _renew_until_stopped(
    identity: _ClaimIdentity,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=_LEASE_RENEW_INTERVAL_SECONDS,
            )
        except TimeoutError:
            pass
        if stop_event.is_set():
            return
        await _renew_import_lease_once(identity)


async def _start_heartbeat(
    identity: _ClaimIdentity,
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Renew immediately, then continue at the configured interval."""
    await _renew_import_lease_once(identity)
    stop_event = asyncio.Event()
    return (
        asyncio.create_task(_renew_until_stopped(identity, stop_event)),
        stop_event,
    )


async def _stop_heartbeat(
    task: asyncio.Task[None],
    stop_event: asyncio.Event,
) -> None:
    stop_event.set()
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _load_customer_validation_inputs(
    session: AsyncSession,
    context: AuthContext,
) -> tuple[dict[str, CustomerIdentity], set[int] | None]:
    customer_index = await data_import_parser.load_customer_name_index(session)
    if context.is_super_admin or context.data_scope == DataScope.ALL:
        return customer_index, None
    statement = apply_customer_scope(
        select(Customer.id).where(
            Customer.status == ContactStatus.ACTIVE,
            Customer.deleted_at.is_(None),
        ),
        context,
    )
    visible_customer_ids = set((await session.scalars(statement)).all())
    return customer_index, visible_customer_ids


async def _validate_source(
    *,
    identity: _ClaimIdentity,
    source_path: Path,
    definition: ResolvedDataDefinition,
    customer_index: dict[str, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
    heartbeat: asyncio.Task[None],
) -> _ValidationSummary:
    iterator = await asyncio.to_thread(
        _row_iterator,
        source=identity.source,
        source_path=source_path,
        definition=definition,
        customer_index=customer_index,
        allowed_customer_ids=allowed_customer_ids,
    )
    total_rows = 0
    valid_rows = 0
    invalid_rows = 0
    error_count = 0
    business_dates: set[date] = set()
    issues: list[RowIssue] = []
    finished = False
    try:
        while not finished:
            batch, finished = await asyncio.to_thread(
                _take_rows,
                iterator,
                _STAGE_BATCH_SIZE,
            )
            _raise_if_heartbeat_failed(heartbeat)
            for parsed in batch:
                total_rows += 1
                if parsed.row is not None and not parsed.issues:
                    valid_rows += 1
                    business_dates.add(parsed.row.business_date)
                else:
                    invalid_rows += 1
                    error_count += len(parsed.issues)
                    remaining = _MAX_ERROR_DETAILS - len(issues)
                    if remaining > 0:
                        issues.extend(parsed.issues[:remaining])
    finally:
        if not finished:
            await asyncio.to_thread(_close_iterator, iterator)

    return _ValidationSummary(
        total_rows=total_rows,
        valid_rows=valid_rows,
        invalid_rows=invalid_rows,
        error_count=error_count,
        business_dates=frozenset(business_dates),
        issues=tuple(issues),
    )


async def _persist_validation_summary(
    session: AsyncSession,
    identity: _ClaimIdentity,
    summary: _ValidationSummary,
) -> None:
    try:
        job = await _lock_claimed_job(
            session,
            identity,
            expected_statuses={DataImportStatus.VALIDATING},
        )
        job.total_rows = summary.total_rows
        job.valid_rows = summary.valid_rows
        job.invalid_rows = summary.invalid_rows
        job.error_count = summary.error_count
        job.date_from = min(summary.business_dates) if summary.business_dates else None
        job.date_to = max(summary.business_dates) if summary.business_dates else None
        job.progress_percent = 20
        session.add_all(
            DataImportError(
                import_id=identity.job_id,
                sheet_name=issue.sheet_name,
                row_number=issue.row_number,
                column_label=issue.column_label,
                field_id=issue.field_id,
                error_code=issue.error_code,
                message=issue.message[:500],
            )
            for issue in summary.issues
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def _stage_source(
    *,
    session: AsyncSession,
    identity: _ClaimIdentity,
    source_path: Path,
    definition: ResolvedDataDefinition,
    customer_index: dict[str, CustomerIdentity],
    allowed_customer_ids: set[int] | None,
    snapshots: dict[date, DataSnapshot],
    expected_dates: frozenset[date],
    expected_rows: int,
    heartbeat: asyncio.Task[None],
) -> None:
    iterator = await asyncio.to_thread(
        _row_iterator,
        source=identity.source,
        source_path=source_path,
        definition=definition,
        customer_index=customer_index,
        allowed_customer_ids=allowed_customer_ids,
    )
    staged_rows = 0
    staged_dates: set[date] = set()
    finished = False
    try:
        while not finished:
            batch, finished = await asyncio.to_thread(
                _take_rows,
                iterator,
                _STAGE_BATCH_SIZE,
            )
            _raise_if_heartbeat_failed(heartbeat)
            rows_by_date: dict[date, list[ValidatedImportRow]] = {}
            for parsed in batch:
                if parsed.row is None or parsed.issues:
                    raise _ImportFailure(
                        ErrorCode.DATA_IMPORT_VALIDATION_FAILED.name,
                        "Import changed after validation",
                    )
                rows_by_date.setdefault(parsed.row.business_date, []).append(parsed.row)
            claimed_job: DataImportJob | None = None
            for business_date, rows in rows_by_date.items():
                snapshot = snapshots.get(business_date)
                if snapshot is None:
                    raise _ImportFailure(
                        ErrorCode.DATA_IMPORT_VALIDATION_FAILED.name,
                        "Import date set changed after validation",
                    )
                _, current_job, _ = await _lock_snapshot_chain(session, snapshot.id)
                _require_claim_identity(
                    current_job,
                    identity,
                    expected_statuses={DataImportStatus.APPLYING},
                )
                claimed_job = current_job
                await append_staged_rows(
                    session,
                    snapshot.id,
                    rows,
                    worker_id=identity.worker_id,
                    batch_size=len(rows),
                )
                staged_dates.add(business_date)
            staged_rows += len(batch)
            if batch:
                if claimed_job is None:
                    raise _invalid_import_error("Import staging batch has no rows")
                claimed_job.progress_percent = min(
                    90,
                    20 + int((staged_rows / expected_rows) * 70),
                )
                _raise_if_heartbeat_failed(heartbeat)
                await session.commit()
    finally:
        if not finished:
            await asyncio.to_thread(_close_iterator, iterator)

    if staged_rows != expected_rows or staged_dates != set(expected_dates):
        raise _ImportFailure(
            ErrorCode.DATA_IMPORT_VALIDATION_FAILED.name,
            "Import result changed before staging completed",
        )


async def _load_live_claim(job_id: int, worker_id: str) -> _ClaimIdentity:
    if not worker_id:
        raise ValueError("worker_id must not be empty")
    async with session_factory() as session:
        job = await data_import_repository.get_import_job(session, job_id)
        if job is None:
            raise _not_found_error()
        _require_live_lease(
            job,
            worker_id,
            expected_status=DataImportStatus.VALIDATING,
        )
        return _claim_identity(job, worker_id)


async def _rehydrate_requester(
    session: AsyncSession,
    requested_by: int,
) -> AuthContext:
    user = await user_repository.get_by_id(session, requested_by)
    if user is None or user.status != UserStatus.ACTIVE:
        raise _ImportFailure(
            ErrorCode.ACCOUNT_DISABLED.name,
            "Import requester is inactive",
        )
    context = await build_auth_context(session, user)
    _require_import_permission(context)
    return context


async def _audit_failure(
    *,
    job_id: int,
    requested_by: int,
    reason: str,
) -> None:
    actor = None
    try:
        async with session_factory() as session:
            user = await user_repository.get_by_id(session, requested_by)
            if user is not None:
                context = await build_auth_context(session, user)
                actor = actor_from_context(context)
    except Exception:
        logger.warning("Could not rehydrate import requester for audit")
    await record_audit_entry(
        action="data_import:fail",
        resource_type="custom_data_import",
        resource_id=job_id,
        result=AuditResult.DENIED,
        reason=reason[:64],
        actor=actor,
        request_meta=None,
    )


async def _mark_failed_and_discard(
    identity: _ClaimIdentity,
    *,
    code: str,
    message: str,
) -> bool:
    marked = False
    try:
        async with session_factory() as session:
            job = await _lock_claimed_job(
                session,
                identity,
                expected_statuses={
                    DataImportStatus.VALIDATING,
                    DataImportStatus.APPLYING,
                },
            )
            job.status = DataImportStatus.FAILED
            job.failure_code = code[:64]
            job.failure_message = message[:500]
            job.finished_at = _utc_now()
            await session.commit()
            marked = True
    except Exception:
        logger.warning("Could not mark data import job %s failed", identity.job_id)
        return False

    try:
        async with session_factory() as session:
            await discard_staged_import(
                session,
                identity.job_id,
                worker_id=identity.worker_id,
                attempt_count=identity.attempt_count,
            )
    except Exception:
        logger.warning(
            "Could not discard staged data for failed import job %s",
            identity.job_id,
        )
    return marked


async def _process_claimed_import(identity: _ClaimIdentity) -> None:
    heartbeat, heartbeat_stop = await _start_heartbeat(identity)
    try:
        async with session_factory() as session:
            context = await _rehydrate_requester(session, identity.requested_by)
            _raise_if_heartbeat_failed(heartbeat)
            definition = await data_table_repository.load_resolved_definition(
                session,
                identity.table_id,
            )
            _raise_if_heartbeat_failed(heartbeat)
            if definition is None:
                raise _ImportFailure(
                    ErrorCode.DATA_TABLE_NOT_FOUND.name,
                    "Import table definition is unavailable",
                )
            (
                customer_index,
                allowed_customer_ids,
            ) = await _load_customer_validation_inputs(session, context)
            _raise_if_heartbeat_failed(heartbeat)

            try:
                job = await _lock_claimed_job(
                    session,
                    identity,
                    expected_statuses={DataImportStatus.VALIDATING},
                )
                job.progress_percent = 5
                await session.commit()
            except Exception:
                await session.rollback()
                raise

            with tempfile.TemporaryDirectory(prefix="data-import-worker-") as root:
                source_path = Path(root) / _source_filename(identity.source)
                await asyncio.to_thread(
                    _materialize_source,
                    storage_path=identity.storage_path,
                    target_path=source_path,
                    expected_sha256=identity.content_sha256,
                    expected_size=identity.size_bytes,
                )
                _raise_if_heartbeat_failed(heartbeat)
                summary = await _validate_source(
                    identity=identity,
                    source_path=source_path,
                    definition=definition,
                    customer_index=customer_index,
                    allowed_customer_ids=allowed_customer_ids,
                    heartbeat=heartbeat,
                )
                _raise_if_heartbeat_failed(heartbeat)
                await _persist_validation_summary(session, identity, summary)
                _raise_if_heartbeat_failed(heartbeat)
                if summary.total_rows == 0:
                    raise _ImportFailure(
                        ErrorCode.DATA_IMPORT_VALIDATION_FAILED.name,
                        "Import contains no data rows",
                    )
                if summary.error_count:
                    raise _ImportFailure(
                        ErrorCode.DATA_IMPORT_VALIDATION_FAILED.name,
                        "Import contains validation errors",
                    )
                if not summary.business_dates:
                    raise _ImportFailure(
                        ErrorCode.DATA_IMPORT_VALIDATION_FAILED.name,
                        "Import contains no business dates",
                    )

                try:
                    claimed_job = await _lock_claimed_job(
                        session,
                        identity,
                        expected_statuses={DataImportStatus.VALIDATING},
                    )
                    claimed_job.progress_percent = 25
                    snapshots = await start_staged_import(
                        session,
                        identity.job_id,
                        summary.business_dates,
                        worker_id=identity.worker_id,
                    )
                except Exception:
                    await session.rollback()
                    raise
                _raise_if_heartbeat_failed(heartbeat)
                await _stage_source(
                    session=session,
                    identity=identity,
                    source_path=source_path,
                    definition=definition,
                    customer_index=customer_index,
                    allowed_customer_ids=allowed_customer_ids,
                    snapshots=snapshots,
                    expected_dates=summary.business_dates,
                    expected_rows=summary.total_rows,
                    heartbeat=heartbeat,
                )
                _raise_if_heartbeat_failed(heartbeat)
    finally:
        await _stop_heartbeat(heartbeat, heartbeat_stop)

    async with session_factory() as session:
        try:
            await _lock_claimed_job(
                session,
                identity,
                expected_statuses={DataImportStatus.APPLYING},
            )
            await activate_import(
                session,
                import_id=identity.job_id,
                worker_id=identity.worker_id,
                expected_dates=summary.business_dates,
            )
        except Exception:
            await session.rollback()
            raise
        await record_audit_entry(
            action="data_import:complete",
            resource_type="custom_data_import",
            resource_id=identity.job_id,
            result=AuditResult.SUCCESS,
            reason=None,
            actor=actor_from_context(context),
            request_meta=None,
        )


async def process_import_job(job_id: int, *, worker_id: str) -> None:
    """Process one already-claimed import owned by ``worker_id``.

    The caller obtains the durable lease with
    ``claim_next_import_job(worker_id=worker_id)``.  This function never claims
    an arbitrary job, so an unclaimed, expired, or foreign job is rejected
    without mutation.  A processing failure owned by this lease transitions to
    FAILED, retains the owner/attempt cleanup identity, and discards only
    STAGED snapshots.
    """
    identity = await _load_live_claim(job_id, worker_id)
    try:
        await _process_claimed_import(identity)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        code, message = _safe_failure(error)
        logger.warning(
            "Data import job %s failed (%s)",
            job_id,
            type(error).__name__,
        )
        marked = await _mark_failed_and_discard(
            identity,
            code=code,
            message=message,
        )
        if marked:
            await _audit_failure(
                job_id=job_id,
                requested_by=identity.requested_by,
                reason=code,
            )


__all__: tuple[str, ...] = (
    "activate_import",
    "append_staged_rows",
    "discard_staged_import",
    "start_staged_import",
    "enqueue_import",
    "process_import_job",
)
