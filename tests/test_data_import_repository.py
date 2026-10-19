"""Import job repository tests against real MySQL locking semantics."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
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
from app.models.customer import Customer
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
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
)
from app.models.user import User
from app.repositories import data_import as data_import_repository
from tests.customer_helpers import TEST_DATABASE_URL, make_customer_row, make_user


async def _make_table(session: AsyncSession, owner: User) -> DataTableDefinition:
    table = DataTableDefinition(
        name=f"import-table-{uuid4().hex[:12]}",
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
    status: DataImportStatus = DataImportStatus.QUEUED,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    attempt_count: int = 0,
) -> DataImportJob:
    job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.EXCEL,
        status=status,
        storage_path=f"imports/{uuid4().hex}.xlsx",
        content_sha256=uuid4().hex + uuid4().hex,
        size_bytes=1024,
        requested_by=owner.id,
        attempt_count=attempt_count,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        heartbeat_at=lease_expires_at,
        started_at=datetime.now(UTC).replace(tzinfo=None)
        if status != DataImportStatus.QUEUED
        else None,
    )
    session.add(job)
    await session.flush()
    return job


async def _delete_test_rows(
    factory: async_sessionmaker[AsyncSession],
    *,
    table_id: int | None,
    user_id: int | None,
) -> None:
    async with factory() as session:
        if table_id is not None:
            record_ids = sa.select(DataRecord.id).where(DataRecord.table_id == table_id)
            await session.execute(
                sa.delete(DataRecordValue).where(
                    DataRecordValue.record_id.in_(record_ids)
                )
            )
            await session.execute(
                sa.delete(DataRecord).where(DataRecord.table_id == table_id)
            )
            await session.execute(
                DataCurrentSnapshot.delete().where(
                    DataCurrentSnapshot.c.table_id == table_id
                )
            )
            await session.execute(
                sa.delete(DataSnapshot).where(DataSnapshot.table_id == table_id)
            )
            await session.execute(
                sa.delete(DataImportError).where(
                    DataImportError.import_id.in_(
                        sa.select(DataImportJob.id).where(
                            DataImportJob.table_id == table_id
                        )
                    )
                )
            )
            await session.execute(
                sa.delete(DataImportJob).where(DataImportJob.table_id == table_id)
            )
            field_ids = sa.select(DataFieldDefinition.id).where(
                DataFieldDefinition.table_id == table_id
            )
            await session.execute(
                sa.delete(DataEnumOption).where(DataEnumOption.field_id.in_(field_ids))
            )
            await session.execute(
                sa.delete(DataFieldDefinition).where(
                    DataFieldDefinition.table_id == table_id
                )
            )
            await session.execute(
                sa.delete(DataTableDefinition).where(DataTableDefinition.id == table_id)
            )
        if user_id is not None:
            await session.execute(
                sa.delete(Customer).where(Customer.owner_user_id == user_id)
            )
            await session.execute(
                sa.delete(DataImportJob).where(DataImportJob.requested_by == user_id)
            )
            await session.execute(sa.delete(User).where(User.id == user_id))
        await session.commit()


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


async def test_create_get_list_and_error_queries(session: AsyncSession) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    job = await data_import_repository.create_import_job(
        session,
        table_id=table.id,
        source=DataImportSource.API,
        original_filename=None,
        storage_path="imports/source.json",
        content_sha256="a" * 64,
        size_bytes=42,
        requested_by=owner.id,
    )
    error = DataImportError(
        import_id=job.id,
        row_number=3,
        column_label="Revenue",
        error_code="invalid_decimal",
        message="Revenue is invalid",
    )
    session.add(error)
    await session.flush()

    assert await data_import_repository.get_import_job(session, job.id) is job
    assert await data_import_repository.get_import_job(session, job.id + 1000) is None
    assert await data_import_repository.list_import_jobs(session, table.id) == [job]
    assert await data_import_repository.list_import_errors(session, job.id) == [error]
    assert await data_import_repository.has_unfinished_import(session, table.id) is True


async def test_create_rejects_second_unfinished_job_for_same_table(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    await _make_job(session, table=table, owner=owner)

    with pytest.raises(BusinessError) as error:
        await data_import_repository.create_import_job(
            session,
            table_id=table.id,
            source=DataImportSource.API,
            storage_path="imports/source.json",
            content_sha256="b" * 64,
            size_bytes=10,
            requested_by=owner.id,
        )

    assert error.value.code == ErrorCode.DATA_IMPORT_IN_PROGRESS
    assert error.value.http_status == 409


async def test_create_concurrent_jobs_serializes_on_table_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    table_id: int | None = None
    user_id: int | None = None
    try:
        async with factory() as setup:
            owner = await make_user(setup)
            table = await _make_table(setup, owner)
            table_id = table.id
            user_id = owner.id
            await setup.commit()

        async def enqueue(path: str) -> DataImportJob | BusinessError:
            async with factory() as enqueue_session:
                try:
                    job = await data_import_repository.create_import_job(
                        enqueue_session,
                        table_id=table_id,
                        source=DataImportSource.API,
                        storage_path=path,
                        content_sha256="c" * 64,
                        size_bytes=10,
                        requested_by=user_id,
                    )
                    await enqueue_session.commit()
                    return job
                except BusinessError as error:
                    await enqueue_session.rollback()
                    return error

        results = await asyncio.gather(
            enqueue("imports/first.json"),
            enqueue("imports/second.json"),
        )
        jobs = [item for item in results if isinstance(item, DataImportJob)]
        errors = [item for item in results if isinstance(item, BusinessError)]
        assert len(jobs) == 1
        assert [item.code for item in errors] == [ErrorCode.DATA_IMPORT_IN_PROGRESS]
    finally:
        await _delete_test_rows(factory, table_id=table_id, user_id=user_id)


async def test_claim_uses_skip_locked_and_claims_another_table(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    table_ids: list[int] = []
    user_id: int | None = None
    try:
        async with factory() as setup:
            owner = await make_user(setup)
            first_table = await _make_table(setup, owner)
            second_table = await _make_table(setup, owner)
            first = await _make_job(setup, table=first_table, owner=owner)
            second = await _make_job(setup, table=second_table, owner=owner)
            table_ids = [first_table.id, second_table.id]
            first_id = first.id
            second_id = second.id
            user_id = owner.id
            await setup.commit()

        async with factory() as holder:
            locked = await holder.scalar(
                sa.select(DataImportJob)
                .where(DataImportJob.id == first_id)
                .with_for_update()
            )
            assert locked is not None
            async with factory() as claimant:
                claimed = await asyncio.wait_for(
                    data_import_repository.claim_next_import_job(
                        claimant,
                        worker_id="worker-b",
                    ),
                    timeout=5,
                )
            assert claimed is not None
            assert claimed.id == second_id
            await holder.rollback()

        async with factory() as verifier:
            first = await verifier.get(DataImportJob, first_id)
            second = await verifier.get(DataImportJob, second_id)
            assert first is not None and first.status == DataImportStatus.QUEUED
            assert second is not None
            assert second.status == DataImportStatus.VALIDATING
            assert second.lease_owner == "worker-b"
            assert second.attempt_count == 1
    finally:
        for table_id in table_ids:
            await _delete_test_rows(factory, table_id=table_id, user_id=None)
        if user_id is not None:
            await _delete_test_rows(factory, table_id=None, user_id=user_id)


async def test_concurrent_claim_has_one_winner(
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

        async def claim(worker_id: str) -> DataImportJob | None:
            async with factory() as claim_session:
                return await data_import_repository.claim_next_import_job(
                    claim_session,
                    worker_id=worker_id,
                )

        results = await asyncio.gather(claim("worker-1"), claim("worker-2"))
        claimed = [item for item in results if item is not None]
        assert len(claimed) == 1
        assert claimed[0].id == job_id
    finally:
        await _delete_test_rows(factory, table_id=table_id, user_id=user_id)


async def test_expired_claim_recovers_lease_and_cleans_staged_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    table_id: int | None = None
    user_id: int | None = None
    try:
        async with factory() as setup:
            owner = await make_user(setup)
            table = await _make_table(setup, owner)
            customer = await make_customer_row(setup, owner=owner)
            job = await _make_job(
                setup,
                table=table,
                owner=owner,
                status=DataImportStatus.VALIDATING,
                lease_owner="stale-worker",
                lease_expires_at=datetime.now(UTC).replace(tzinfo=None)
                - timedelta(minutes=1),
                attempt_count=1,
            )
            job.total_rows = 20
            job.valid_rows = 18
            job.invalid_rows = 2
            job.date_from = date(2026, 10, 17)
            job.date_to = date(2026, 10, 18)
            job.error_count = 2
            job.progress_percent = 75
            snapshot = DataSnapshot(
                table_id=table.id,
                business_date=date(2026, 10, 18),
                import_id=job.id,
                version=1,
                status=DataSnapshotStatus.STAGED,
            )
            setup.add(snapshot)
            await setup.flush()
            record = DataRecord(
                snapshot_id=snapshot.id,
                table_id=table.id,
                business_date=snapshot.business_date,
                upstream_customer_id=customer.id,
                downstream_customer_id=customer.id,
                upstream_customer_name_source=customer.name,
                downstream_customer_name_source=customer.name,
                revenue=1,
                cost=1,
            )
            setup.add(record)
            await setup.flush()
            table_id = table.id
            user_id = owner.id
            job_id = job.id
            snapshot_id = snapshot.id
            await setup.commit()

        async with factory() as claimant:
            claimed = await data_import_repository.claim_next_import_job(
                claimant,
                worker_id="fresh-worker",
                lease_seconds=120,
            )
            assert claimed is not None
            assert claimed.id == job_id
            assert claimed.attempt_count == 2
            assert claimed.lease_owner == "fresh-worker"
            assert claimed.total_rows == 0
            assert claimed.valid_rows == 0
            assert claimed.invalid_rows == 0
            assert claimed.date_from is None
            assert claimed.date_to is None
            assert claimed.error_count == 0
            assert claimed.progress_percent == 0

        async with factory() as verifier:
            assert await verifier.get(DataSnapshot, snapshot_id) is None
            refreshed = await verifier.get(DataImportJob, job_id)
            assert refreshed is not None
            assert refreshed.status == DataImportStatus.VALIDATING
            assert refreshed.date_from is None
            assert refreshed.date_to is None
    finally:
        await _delete_test_rows(factory, table_id=table_id, user_id=user_id)


async def test_renew_lease_requires_owner_and_unexpired_active_lease(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.VALIDATING,
        lease_owner="worker-a",
        lease_expires_at=expiry,
        attempt_count=1,
    )

    assert (
        await data_import_repository.renew_import_lease(
            session,
            import_id=job.id,
            worker_id="worker-b",
            lease_seconds=300,
            attempt_count=1,
        )
        is False
    )
    assert (
        await data_import_repository.renew_import_lease(
            session,
            import_id=job.id,
            worker_id="worker-a",
            lease_seconds=300,
            attempt_count=2,
        )
        is False
    )
    assert (
        await data_import_repository.renew_import_lease(
            session,
            import_id=job.id,
            worker_id="worker-a",
            lease_seconds=300,
            attempt_count=1,
        )
        is True
    )
    await session.refresh(job)
    assert job.lease_expires_at is not None
    assert job.lease_expires_at > expiry

    job.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    await session.flush()
    assert (
        await data_import_repository.renew_import_lease(
            session,
            import_id=job.id,
            worker_id="worker-a",
            lease_seconds=300,
        )
        is False
    )


async def test_list_current_snapshots_reads_pointer_not_highest_version(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    old_job = await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    selected = DataSnapshot(
        table_id=table.id,
        business_date=date(2026, 10, 18),
        import_id=old_job.id,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
    )
    session.add(selected)
    await session.flush()
    session.add(
        DataSnapshot(
            table_id=table.id,
            business_date=date(2026, 10, 18),
            import_id=old_job.id,
            version=2,
            status=DataSnapshotStatus.SUPERSEDED,
        )
    )
    await session.execute(
        DataCurrentSnapshot.insert().values(
            table_id=table.id,
            business_date=selected.business_date,
            snapshot_id=selected.id,
        )
    )
    await session.flush()

    result = await data_import_repository.list_current_snapshots(session, table.id)

    assert [item.id for item in result] == [selected.id]


async def test_terminal_jobs_are_never_claimed(session: AsyncSession) -> None:
    owner = await make_user(session)
    table = await _make_table(session, owner)
    await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.SUCCEEDED,
    )
    await _make_job(
        session,
        table=table,
        owner=owner,
        status=DataImportStatus.FAILED,
    )

    assert (
        await data_import_repository.claim_next_import_job(
            session,
            worker_id="worker-a",
        )
        is None
    )
