"""数据表定义仓储层测试。

覆盖解析后的定义结构、加锁语义、DRAFT 生命周期、保存点名称冲突映射、
失败导入诊断的降级处理以及物理删除的依赖顺序。
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.dialects import mysql
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
from app.models.user import User
from app.repositories import data_table as data_table_repository
from app.repositories.data_table import EnumOptionSpec, FieldSpec
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_customer_row,
    make_user,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """单事务会话，测试结束回滚。"""
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


def fixed_field_specs() -> list[FieldSpec]:
    return [
        FieldSpec(
            label="日期",
            field_type=DataFieldType.DATE,
            system_key=DataFieldRole.BUSINESS_DATE,
            required=True,
            sort_order=0,
        ),
        FieldSpec(
            label="上游客户",
            field_type=DataFieldType.CUSTOMER,
            system_key=DataFieldRole.UPSTREAM_CUSTOMER,
            required=True,
            sort_order=1,
        ),
        FieldSpec(
            label="下游客户",
            field_type=DataFieldType.CUSTOMER,
            system_key=DataFieldRole.DOWNSTREAM_CUSTOMER,
            required=True,
            sort_order=2,
        ),
        FieldSpec(
            label="收入",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.REVENUE,
            required=True,
            sort_order=3,
        ),
        FieldSpec(
            label="成本",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.COST,
            required=True,
            sort_order=4,
        ),
    ]


def custom_spec(
    label: str = "备注",
    *,
    field_type: DataFieldType = DataFieldType.TEXT,
    required: bool = False,
    sort_order: int = 10,
    options: tuple[EnumOptionSpec, ...] = (),
) -> FieldSpec:
    return FieldSpec(
        label=label,
        field_type=field_type,
        system_key=DataFieldRole.CUSTOM,
        required=required,
        sort_order=sort_order,
        options=options,
    )


async def add_table(
    session: AsyncSession,
    *,
    created_by: int,
    name: str | None = None,
    fields: list[FieldSpec] | None = None,
    description: str | None = None,
) -> DataTableDefinition:
    return await data_table_repository.create_table(
        session,
        name=name or f"仓储表-{uuid4().hex[:8]}",
        description=description,
        fields=list(fixed_field_specs() if fields is None else fields),
        created_by=created_by,
    )


async def add_import(
    session: AsyncSession,
    table: DataTableDefinition,
    *,
    status: DataImportStatus,
    owner_id: int,
) -> DataImportJob:
    job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.API,
        status=status,
        storage_path=f"imports/{uuid4().hex}.json",
        content_sha256=uuid4().hex * 2,
        size_bytes=64,
        requested_by=owner_id,
    )
    session.add(job)
    await session.flush()
    return job


async def _locked_row(
    session: AsyncSession,
    table_id: int,
) -> DataTableDefinition:
    row = await data_table_repository.get_table_for_update(session, table_id)
    assert row is not None
    return row


async def test_get_table_returns_row_or_none(session: AsyncSession) -> None:
    user = await make_user(session)
    table = await add_table(session, created_by=user.id, name="查表甲")

    loaded = await data_table_repository.get_table(session, table.id)

    assert loaded is not None
    assert loaded.id == table.id
    assert loaded.name == "查表甲"
    assert loaded.status == DataTableStatus.DRAFT
    assert await data_table_repository.get_table(session, table.id + 1000) is None


async def test_get_table_for_update_compiles_current_read(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(session, created_by=user.id)

    loaded = await data_table_repository.get_table_for_update(session, table.id)

    assert loaded is not None and loaded.id == table.id
    assert (
        await data_table_repository.get_table_for_update(session, table.id + 1000)
        is None
    )


def test_get_table_for_update_sql_uses_for_update() -> None:
    statement = data_table_repository.build_table_for_update_statement(10)
    compiled = str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "custom_data_table.id = 10" in compiled
    assert "FOR UPDATE" in compiled
    options = statement.get_execution_options()
    assert options["populate_existing"] is True


async def test_get_table_for_update_blocks_concurrent_writer() -> None:
    """第二个事务必须等第一个持锁事务提交后才能读到新定义。"""
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    table_id: int | None = None
    user_id: int | None = None
    observed: list[str] = []

    try:
        async with factory() as setup:
            user = await make_user(setup)
            table = await add_table(setup, created_by=user.id, name="并发锁测试")
            user_id = user.id
            table_id = table.id
            await setup.commit()

        async with factory() as holder:
            locked = await data_table_repository.get_table_for_update(
                holder,
                table_id,
            )
            assert locked is not None

            async def competing_reader() -> None:
                await asyncio.sleep(0.05)
                async with factory() as reader:
                    row = await data_table_repository.get_table_for_update(
                        reader,
                        table_id,
                    )
                    assert row is not None
                    # 必须在会话内取值：回滚会过期化已加载实例的属性。
                    name = row.name
                    await reader.rollback()
                observed.append(name)

            task = asyncio.create_task(competing_reader())
            await asyncio.sleep(0.3)
            assert not task.done()
            locked.name = "持锁后的新名称"
            await holder.commit()
            await asyncio.wait_for(task, timeout=10)

        # 竞争者只有在持锁事务提交后才能读到，并且看到的是提交后的新值。
        assert observed == ["持锁后的新名称"]
    finally:
        async with factory() as cleanup:
            if table_id is not None:
                row = await data_table_repository.get_table_for_update(
                    cleanup,
                    table_id,
                )
                if row is not None:
                    await data_table_repository.delete_table(cleanup, row)
            if user_id is not None:
                await cleanup.execute(User.__table__.delete().where(User.id == user_id))
            await cleanup.commit()
        await engine.dispose()


async def test_load_resolved_definition_builds_maps(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(
        session,
        created_by=user.id,
        fields=[
            *fixed_field_specs(),
            custom_spec("备注", required=True, sort_order=10),
            custom_spec(
                "渠道",
                field_type=DataFieldType.ENUM,
                sort_order=11,
                options=(
                    EnumOptionSpec(label="自然", sort_order=1),
                    EnumOptionSpec(label="付费", sort_order=0),
                ),
            ),
        ],
    )

    definition = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )

    assert definition is not None
    assert definition.table.id == table.id
    assert len(definition.fields) == 7
    assert set(definition.fields_by_id) == {field.id for field in definition.fields}
    assert set(definition.fields_by_system_key) == {
        DataFieldRole.BUSINESS_DATE,
        DataFieldRole.UPSTREAM_CUSTOMER,
        DataFieldRole.DOWNSTREAM_CUSTOMER,
        DataFieldRole.REVENUE,
        DataFieldRole.COST,
    }
    assert DataFieldRole.CUSTOM not in definition.fields_by_system_key
    assert definition.labels == (
        "日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
        "备注",
        "渠道",
    )
    assert [field.label for field in definition.custom_fields] == ["备注", "渠道"]

    enum_row = next(
        field
        for field in definition.custom_fields
        if field.field_type == DataFieldType.ENUM
    )
    options = definition.options_for(enum_row.id)
    assert [option.label for option in options] == ["付费", "自然"]
    assert set(definition.enum_options_by_id) == {option.id for option in options}
    assert definition.enum_options_by_field_id == {enum_row.id: options}
    assert definition.options_for(enum_row.id + 1000) == ()
    assert definition.field_for(DataFieldRole.BUSINESS_DATE) is not None


async def test_load_resolved_definition_returns_none_for_missing_table(
    session: AsyncSession,
) -> None:
    assert await data_table_repository.load_resolved_definition(session, 0) is None


async def test_load_resolved_definition_orders_by_sort_order_then_id(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(
        session,
        created_by=user.id,
        fields=[
            *fixed_field_specs(),
            custom_spec("后建先排", sort_order=5),
            custom_spec("后建后排", sort_order=5),
            custom_spec("最末", sort_order=99),
        ],
    )

    definition = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )

    assert definition is not None
    assert [field.label for field in definition.fields] == [
        "日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
        "后建先排",
        "后建后排",
        "最末",
    ]
    assert definition.labels == tuple(field.label for field in definition.fields)


async def test_list_tables_returns_all_rows_in_id_order(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    first = await add_table(session, created_by=user.id)
    second = await add_table(session, created_by=user.id)

    tables = await data_table_repository.list_tables(session)

    ids = [table.id for table in tables]
    assert ids == sorted(ids)
    assert {first.id, second.id} <= set(ids)


async def test_create_table_persists_fields_and_options_in_order(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(
        session,
        created_by=user.id,
        fields=[
            *fixed_field_specs(),
            custom_spec("备注", required=True, sort_order=10),
            custom_spec(
                "渠道",
                field_type=DataFieldType.ENUM,
                sort_order=11,
                options=(EnumOptionSpec(label="A", sort_order=0),),
            ),
        ],
    )

    definition = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert definition is not None
    assert definition.table.id == table.id
    assert definition.table.created_by == user.id
    assert [field.field_type for field in definition.fields] == [
        DataFieldType.DATE,
        DataFieldType.CUSTOMER,
        DataFieldType.CUSTOMER,
        DataFieldType.DECIMAL,
        DataFieldType.DECIMAL,
        DataFieldType.TEXT,
        DataFieldType.ENUM,
    ]
    custom = definition.custom_fields[0]
    assert custom.required is True
    enum_row = definition.custom_fields[1]
    assert [option.label for option in definition.options_for(enum_row.id)] == ["A"]


async def test_create_table_maps_duplicate_name_to_9002(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    await add_table(session, created_by=user.id, name="重复名称表")

    with pytest.raises(BusinessError) as exc:
        await add_table(session, created_by=user.id, name="重复名称表")

    assert exc.value.code == ErrorCode.DATA_TABLE_NAME_EXISTS
    assert exc.value.http_status == 409
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataTableDefinition)
            .where(DataTableDefinition.name == "重复名称表")
        )
        == 1
    )


async def test_create_table_maps_duplicate_label_to_9004(
    session: AsyncSession,
) -> None:
    """进程内校验之外，数据库唯一索引兜底也要映射为 9004 而非 500。"""
    user = await make_user(session)

    with pytest.raises(BusinessError) as exc:
        await add_table(
            session,
            created_by=user.id,
            fields=[
                *fixed_field_specs(),
                custom_spec("同名标签"),
                custom_spec("同名标签"),
            ],
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID
    assert exc.value.http_status == 422
    assert (
        await session.scalar(select(func.count()).select_from(DataTableDefinition)) >= 0
    )


async def test_create_table_maps_duplicate_system_role_to_9004(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    fields = [
        *fixed_field_specs(),
        FieldSpec(
            label="第二个收入",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.REVENUE,
            required=True,
            sort_order=9,
        ),
    ]

    with pytest.raises(BusinessError) as exc:
        await add_table(session, created_by=user.id, fields=fields)

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID
    assert exc.value.http_status == 422


async def test_replace_table_requires_draft_status(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(session, created_by=user.id)
    table.status = DataTableStatus.LOCKED
    table.locked_at = datetime.now(UTC)
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await data_table_repository.replace_table(
            session,
            await _locked_row(session, table.id),
            name="冻结后替换",
            description=None,
            fields=fixed_field_specs(),
            updated_by=user.id,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_LOCKED


async def test_replace_table_replaces_fields_and_options(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(
        session,
        created_by=user.id,
        description="旧描述",
        fields=[
            *fixed_field_specs(),
            custom_spec("旧字段", sort_order=10),
            custom_spec(
                "旧枚举",
                field_type=DataFieldType.ENUM,
                sort_order=11,
                options=(EnumOptionSpec(label="旧选项", sort_order=0),),
            ),
        ],
    )
    before = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert before is not None
    old_field_ids = set(before.fields_by_id)
    old_option_ids = set(before.enum_options_by_id)

    locked = await data_table_repository.get_table_for_update(session, table.id)
    assert locked is not None
    replaced = await data_table_repository.replace_table(
        session,
        locked,
        name="仓储改名",
        description="新描述",
        fields=[*fixed_field_specs(), custom_spec("新字段", sort_order=20)],
        updated_by=user.id,
    )

    after = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert after is not None
    assert replaced.id == table.id
    assert replaced.name == "仓储改名"
    assert replaced.description == "新描述"
    assert replaced.updated_by == user.id
    assert set(after.fields_by_id).isdisjoint(old_field_ids)
    assert set(after.enum_options_by_id).isdisjoint(old_option_ids)
    assert [field.label for field in after.custom_fields] == ["新字段"]
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataEnumOption)
            .where(DataEnumOption.field_id.in_(old_field_ids))
        )
        == 0
    )


async def test_replace_table_name_conflict_keeps_previous_definition(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    first = await add_table(session, created_by=user.id, name="占位名称")
    second = await add_table(
        session,
        created_by=user.id,
        name="待替换名称",
        fields=[*fixed_field_specs(), custom_spec("保留字段", sort_order=10)],
    )
    before = await data_table_repository.load_resolved_definition(
        session,
        second.id,
    )
    assert before is not None
    before_ids = set(before.fields_by_id)

    locked = await data_table_repository.get_table_for_update(session, second.id)
    assert locked is not None
    with pytest.raises(BusinessError) as exc:
        await data_table_repository.replace_table(
            session,
            locked,
            name="占位名称",
            description=None,
            fields=fixed_field_specs(),
            updated_by=user.id,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_NAME_EXISTS
    after = await data_table_repository.load_resolved_definition(
        session,
        second.id,
    )
    assert after is not None
    assert after.table.name == "待替换名称"
    assert set(after.fields_by_id) == before_ids
    assert [field.label for field in after.custom_fields] == ["保留字段"]
    assert first.id != second.id


async def test_replace_table_clears_import_error_field_references(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(
        session,
        created_by=user.id,
        fields=[*fixed_field_specs(), custom_spec("备注", sort_order=10)],
    )
    definition = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert definition is not None
    custom = definition.custom_fields[0]
    job = await add_import(
        session,
        table,
        status=DataImportStatus.FAILED,
        owner_id=user.id,
    )
    error = DataImportError(
        import_id=job.id,
        row_number=4,
        column_label="备注",
        field_id=custom.id,
        error_code="TEXT_TOO_LONG",
        message="文本超出上限",
    )
    session.add(error)
    await session.flush()

    locked = await data_table_repository.get_table_for_update(session, table.id)
    assert locked is not None
    await data_table_repository.replace_table(
        session,
        locked,
        name=table.name,
        description=None,
        fields=[*fixed_field_specs(), custom_spec("新备注", sort_order=10)],
        updated_by=user.id,
    )

    refreshed = await session.get(DataImportError, error.id)
    assert refreshed is not None
    assert refreshed.field_id is None
    assert refreshed.column_label == "备注"
    assert refreshed.row_number == 4


async def test_replace_table_discards_staged_rows_before_rebuilding(
    session: AsyncSession,
) -> None:
    """崩溃重试可能留下 STAGED 数据，它们引用旧字段，必须先清理再重建。"""
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    table = await add_table(
        session,
        created_by=user.id,
        fields=[
            *fixed_field_specs(),
            custom_spec(
                "渠道",
                field_type=DataFieldType.ENUM,
                sort_order=10,
                options=(EnumOptionSpec(label="A", sort_order=0),),
            ),
        ],
    )
    definition = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert definition is not None
    enum_row = definition.custom_fields[0]
    option = definition.options_for(enum_row.id)[0]
    job = await add_import(
        session,
        table,
        status=DataImportStatus.FAILED,
        owner_id=user.id,
    )
    snapshot = DataSnapshot(
        table_id=table.id,
        business_date=date(2026, 10, 18),
        import_id=job.id,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    session.add(snapshot)
    await session.flush()
    record = DataRecord(
        snapshot_id=snapshot.id,
        table_id=table.id,
        business_date=date(2026, 10, 18),
        upstream_customer_id=customer.id,
        downstream_customer_id=customer.id,
        upstream_customer_name_source="上游来源",
        downstream_customer_name_source="下游来源",
        revenue=Decimal("10.00000000"),
        cost=Decimal("4.00000000"),
    )
    session.add(record)
    await session.flush()
    session.add(
        DataRecordValue(
            record_id=record.id,
            field_id=enum_row.id,
            enum_option_id=option.id,
        )
    )
    await session.flush()

    locked = await data_table_repository.get_table_for_update(session, table.id)
    assert locked is not None
    await data_table_repository.replace_table(
        session,
        locked,
        name=table.name,
        description=None,
        fields=[*fixed_field_specs(), custom_spec("新渠道", sort_order=10)],
        updated_by=user.id,
    )

    assert await session.get(DataRecord, record.id) is None
    assert await session.get(DataSnapshot, snapshot.id) is None
    after = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert after is not None
    assert [field.label for field in after.custom_fields] == ["新渠道"]


async def test_has_unfinished_import_covers_active_statuses(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(session, created_by=user.id)
    other = await add_table(session, created_by=user.id)
    await add_import(
        session,
        other,
        status=DataImportStatus.QUEUED,
        owner_id=user.id,
    )

    assert await data_table_repository.has_unfinished_import(session, table.id) is False

    for status in (
        DataImportStatus.QUEUED,
        DataImportStatus.VALIDATING,
        DataImportStatus.APPLYING,
    ):
        job = await add_import(
            session,
            table,
            status=status,
            owner_id=user.id,
        )
        assert (
            await data_table_repository.has_unfinished_import(session, table.id) is True
        )
        job.status = DataImportStatus.FAILED
        await session.flush()

    for status in (DataImportStatus.SUCCEEDED, DataImportStatus.FAILED):
        job = await add_import(
            session,
            table,
            status=status,
            owner_id=user.id,
        )
        assert (
            await data_table_repository.has_unfinished_import(session, table.id)
            is False
        )
        assert job.id > 0


async def test_delete_table_requires_draft(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(session, created_by=user.id)
    table.status = DataTableStatus.LOCKED
    table.locked_at = datetime.now(UTC)
    await session.flush()

    locked = await data_table_repository.get_table_for_update(session, table.id)
    assert locked is not None
    with pytest.raises(BusinessError) as exc:
        await data_table_repository.delete_table(session, locked)

    assert exc.value.code == ErrorCode.DATA_TABLE_LOCKED
    assert await session.get(DataTableDefinition, table.id) is not None


async def test_delete_table_removes_dependents_in_safe_order(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    table = await add_table(
        session,
        created_by=user.id,
        fields=[
            *fixed_field_specs(),
            custom_spec(
                "渠道",
                field_type=DataFieldType.ENUM,
                sort_order=10,
                options=(EnumOptionSpec(label="A", sort_order=0),),
            ),
        ],
    )
    definition = await data_table_repository.load_resolved_definition(
        session,
        table.id,
    )
    assert definition is not None
    enum_row = definition.custom_fields[0]
    option = definition.options_for(enum_row.id)[0]
    job = await add_import(
        session,
        table,
        status=DataImportStatus.FAILED,
        owner_id=user.id,
    )
    snapshot = DataSnapshot(
        table_id=table.id,
        business_date=date(2026, 10, 18),
        import_id=job.id,
        version=1,
        status=DataSnapshotStatus.STAGED,
    )
    session.add(snapshot)
    await session.flush()
    record = DataRecord(
        snapshot_id=snapshot.id,
        table_id=table.id,
        business_date=date(2026, 10, 18),
        upstream_customer_id=customer.id,
        downstream_customer_id=customer.id,
        upstream_customer_name_source="上游来源",
        downstream_customer_name_source="下游来源",
        revenue=Decimal("10.00000000"),
        cost=Decimal("4.00000000"),
    )
    session.add(record)
    await session.flush()
    session.add(
        DataRecordValue(
            record_id=record.id,
            field_id=enum_row.id,
            enum_option_id=option.id,
        )
    )
    error = DataImportError(
        import_id=job.id,
        field_id=enum_row.id,
        column_label="渠道",
        error_code="ENUM_UNKNOWN",
        message="未知枚举选项",
    )
    session.add(error)
    await session.flush()
    await session.execute(
        DataCurrentSnapshot.insert().values(
            table_id=table.id,
            business_date=date(2026, 10, 18),
            snapshot_id=snapshot.id,
        )
    )

    locked = await data_table_repository.get_table_for_update(session, table.id)
    assert locked is not None
    await data_table_repository.delete_table(session, locked)

    assert await session.get(DataTableDefinition, table.id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataFieldDefinition)
            .where(DataFieldDefinition.table_id == table.id)
        )
        == 0
    )
    assert await session.get(DataImportJob, job.id) is None
    assert await session.get(DataSnapshot, snapshot.id) is None
    assert await session.get(DataRecord, record.id) is None
    assert await session.get(DataImportError, error.id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataEnumOption)
            .where(DataEnumOption.field_id == enum_row.id)
        )
        == 0
    )


async def test_delete_table_removes_table_without_history(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    table = await add_table(session, created_by=user.id)

    locked = await data_table_repository.get_table_for_update(session, table.id)
    assert locked is not None
    await data_table_repository.delete_table(session, locked)

    assert await session.get(DataTableDefinition, table.id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataFieldDefinition)
            .where(DataFieldDefinition.table_id == table.id)
        )
        == 0
    )
