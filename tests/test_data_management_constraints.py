"""真实 MySQL 约束验证：生成列与唯一索引的实际行为。

模型与迁移的 SQL 文本断言见 ``test_data_management_models.py`` 和
``test_data_management_migration.py``；本模块只验证数据库真正执行的约束。
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.data_management import (
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportJob,
    DataImportSource,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
)
from app.models.user import User, UserStatus

TEST_DATABASE_URL: str = os.environ["DATABASE_URL"]

BUSINESS_DATE = date(2026, 10, 18)
OTHER_BUSINESS_DATE = date(2026, 10, 19)
FIXED_SYSTEM_KEYS = (
    DataFieldRole.BUSINESS_DATE,
    DataFieldRole.UPSTREAM_CUSTOMER,
    DataFieldRole.DOWNSTREAM_CUSTOMER,
    DataFieldRole.REVENUE,
    DataFieldRole.COST,
)


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


def _duplicate_key(error: IntegrityError) -> int:
    assert error.orig is not None
    return int(error.orig.args[0])


async def _make_user(session: AsyncSession) -> User:
    user = User(
        username=f"data-constraints-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_table(session: AsyncSession, owner: User) -> DataTableDefinition:
    table = DataTableDefinition(
        name=f"表-{uuid4().hex[:12]}",
        created_by=owner.id,
    )
    session.add(table)
    await session.flush()
    return table


async def _make_import(
    session: AsyncSession,
    *,
    table: DataTableDefinition,
    owner: User,
) -> DataImportJob:
    job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.EXCEL,
        storage_path=f"imports/{uuid4().hex}.xlsx",
        content_sha256=uuid4().hex + uuid4().hex,
        size_bytes=1024,
        requested_by=owner.id,
    )
    session.add(job)
    await session.flush()
    return job


async def test_only_one_active_snapshot_is_allowed_per_table_and_date(
    session: AsyncSession,
) -> None:
    owner = await _make_user(session)
    table = await _make_table(session, owner)
    job = await _make_import(session, table=table, owner=owner)

    staged = DataSnapshot(
        table_id=table.id,
        business_date=BUSINESS_DATE,
        import_id=job.id,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    session.add(staged)
    await session.flush()

    active = DataSnapshot(
        table_id=table.id,
        business_date=BUSINESS_DATE,
        import_id=job.id,
        version=2,
        status=DataSnapshotStatus.ACTIVE,
    )
    session.add(active)
    await session.flush()

    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            session.add(
                DataSnapshot(
                    table_id=table.id,
                    business_date=BUSINESS_DATE,
                    import_id=job.id,
                    version=3,
                    status=DataSnapshotStatus.ACTIVE,
                )
            )
            await session.flush()

    assert _duplicate_key(error.value) == 1062
    assert "uq_custom_data_snapshot_table_active_date" in str(error.value)

    # 多个 STAGED / SUPERSEDED 行仍然合法：生成列为 NULL，不参与唯一约束。
    session.add_all(
        [
            DataSnapshot(
                table_id=table.id,
                business_date=BUSINESS_DATE,
                import_id=job.id,
                version=4,
                status=DataSnapshotStatus.STAGED,
            ),
            DataSnapshot(
                table_id=table.id,
                business_date=BUSINESS_DATE,
                import_id=job.id,
                version=5,
                status=DataSnapshotStatus.SUPERSEDED,
            ),
            DataSnapshot(
                table_id=table.id,
                business_date=BUSINESS_DATE,
                import_id=job.id,
                version=6,
                status=DataSnapshotStatus.SUPERSEDED,
            ),
        ]
    )
    await session.flush()

    # 另一张表、同一日期可以各有一个 ACTIVE；同一表的不同日期同理。
    other_table = await _make_table(session, owner)
    other_job = await _make_import(session, table=other_table, owner=owner)
    other_active = DataSnapshot(
        table_id=other_table.id,
        business_date=BUSINESS_DATE,
        import_id=other_job.id,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
    )
    later_active = DataSnapshot(
        table_id=table.id,
        business_date=OTHER_BUSINESS_DATE,
        import_id=job.id,
        version=1,
        status=DataSnapshotStatus.ACTIVE,
    )
    session.add_all([other_active, later_active])
    await session.flush()

    generated = dict(
        (
            await session.execute(
                sa.select(DataSnapshot.id, DataSnapshot.active_business_key)
            )
        ).all()
    )
    assert generated[staged.id] is None
    assert generated[active.id] == BUSINESS_DATE
    assert generated[other_active.id] == BUSINESS_DATE
    assert generated[later_active.id] == OTHER_BUSINESS_DATE


async def test_generated_active_key_is_null_for_staged_and_superseded_rows(
    session: AsyncSession,
) -> None:
    owner = await _make_user(session)
    table = await _make_table(session, owner)
    job = await _make_import(session, table=table, owner=owner)

    staged = DataSnapshot(
        table_id=table.id,
        business_date=BUSINESS_DATE,
        import_id=job.id,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    superseded = DataSnapshot(
        table_id=table.id,
        business_date=BUSINESS_DATE,
        import_id=job.id,
        version=2,
        status=DataSnapshotStatus.SUPERSEDED,
    )
    session.add_all([staged, superseded])
    await session.flush()

    rows = (
        await session.execute(
            sa.select(DataSnapshot.status, DataSnapshot.active_business_key)
        )
    ).all()
    assert rows == [
        (DataSnapshotStatus.STAGED, None),
        (DataSnapshotStatus.SUPERSEDED, None),
    ]


async def test_fixed_system_keys_are_unique_while_custom_repeats(
    session: AsyncSession,
) -> None:
    owner = await _make_user(session)
    table = await _make_table(session, owner)

    for role in FIXED_SYSTEM_KEYS:
        session.add(
            DataFieldDefinition(
                table_id=table.id,
                label=role.value,
                field_type=DataFieldType.TEXT,
                system_key=role,
            )
        )
    await session.flush()

    session.add_all(
        [
            DataFieldDefinition(
                table_id=table.id,
                label=f"自定义-{index}",
                field_type=DataFieldType.TEXT,
                system_key=DataFieldRole.CUSTOM,
            )
            for index in range(3)
        ]
    )
    await session.flush()

    generated = dict(
        (
            await session.execute(
                sa.select(
                    DataFieldDefinition.system_key,
                    DataFieldDefinition.system_key_unique,
                )
            )
        ).all()
    )
    for role in FIXED_SYSTEM_KEYS:
        assert generated[role] == role.value
    assert generated[DataFieldRole.CUSTOM] is None

    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            session.add(
                DataFieldDefinition(
                    table_id=table.id,
                    label="重复收入",
                    field_type=DataFieldType.DECIMAL,
                    system_key=DataFieldRole.REVENUE,
                )
            )
            await session.flush()

    assert _duplicate_key(error.value) == 1062
    assert "uq_custom_data_field_table_system_key" in str(error.value)

    # 另一张表可以复用同样的固定 system key。
    other_table = await _make_table(session, owner)
    session.add(
        DataFieldDefinition(
            table_id=other_table.id,
            label=DataFieldRole.REVENUE.value,
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.REVENUE,
        )
    )
    await session.flush()


async def test_enum_option_field_id_has_no_redundant_standalone_index(
    session: AsyncSession,
) -> None:
    index_names = set(
        (
            await session.execute(
                sa.text(
                    """
                    SELECT DISTINCT INDEX_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'custom_data_enum_option'
                    """
                )
            )
        ).scalars()
    )

    assert "uq_custom_data_enum_option_field_label" in index_names
    assert "ix_custom_data_enum_option_field_id" not in index_names
