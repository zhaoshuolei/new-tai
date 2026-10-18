"""数据表定义服务测试。

覆盖固定系统字段、类型/标签/枚举选项校验、数量边界、名称冲突、
DRAFT/LOCKED 生命周期、未完成导入互斥、物理删除、提交边界与审计。
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog
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
from app.repositories import data_table as data_table_repository
from app.repositories.data_table import ResolvedDataDefinition
from app.schemas.data_table import (
    DataEnumOptionInput,
    DataFieldInput,
    DataTableCreate,
    DataTableDefinitionRead,
    DataTableReplace,
)
from app.services import audit as audit_service
from app.services import data_table as data_table_service
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_context,
    make_customer_row,
    make_user,
)

_AUDIT_ACTIONS = ("data_table:create", "data_table:update", "data_table:delete")


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """单事务会话，测试结束回滚；服务内的 commit 落在保存点中。"""
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


@pytest_asyncio.fixture(autouse=True)
async def _isolate_audit_rows() -> AsyncIterator[None]:
    """服务写入成功后会在独立事务写审计，测试前后清理这三类动作。"""
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def clear() -> None:
        async with factory() as audit_session:
            await audit_session.execute(
                delete(AuditLog).where(AuditLog.action.in_(_AUDIT_ACTIONS))
            )
            await audit_session.commit()

    await clear()
    try:
        yield
    finally:
        await clear()
        await engine.dispose()


@pytest_asyncio.fixture
async def context(session: AsyncSession) -> AuthContext:
    user = await make_user(session)
    return make_context(
        user,
        permission_codes={
            "data:read",
            "data:create",
            "data:update",
            "data:delete",
            "data:import",
        },
        is_super_admin=True,
    )


def fixed_field_inputs() -> list[DataFieldInput]:
    return [
        DataFieldInput(
            label="日期",
            field_type=DataFieldType.DATE,
            system_key=DataFieldRole.BUSINESS_DATE,
            sort_order=0,
        ),
        DataFieldInput(
            label="上游客户",
            field_type=DataFieldType.CUSTOMER,
            system_key=DataFieldRole.UPSTREAM_CUSTOMER,
            sort_order=1,
        ),
        DataFieldInput(
            label="下游客户",
            field_type=DataFieldType.CUSTOMER,
            system_key=DataFieldRole.DOWNSTREAM_CUSTOMER,
            sort_order=2,
        ),
        DataFieldInput(
            label="收入",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.REVENUE,
            sort_order=3,
        ),
        DataFieldInput(
            label="成本",
            field_type=DataFieldType.DECIMAL,
            system_key=DataFieldRole.COST,
            sort_order=4,
        ),
    ]


def custom_field(
    label: str = "备注",
    *,
    field_type: DataFieldType = DataFieldType.TEXT,
    required: bool = False,
    sort_order: int = 10,
    options: Sequence[DataEnumOptionInput] = (),
) -> DataFieldInput:
    return DataFieldInput(
        label=label,
        field_type=field_type,
        system_key=DataFieldRole.CUSTOM,
        required=required,
        sort_order=sort_order,
        options=list(options),
    )


def enum_field(
    label: str = "渠道",
    *,
    options: Sequence[DataEnumOptionInput] = (),
    required: bool = False,
    sort_order: int = 10,
) -> DataFieldInput:
    return custom_field(
        label,
        field_type=DataFieldType.ENUM,
        required=required,
        sort_order=sort_order,
        options=options
        or [
            DataEnumOptionInput(label="A", sort_order=0),
            DataEnumOptionInput(label="B", sort_order=1),
        ],
    )


def make_table_payload(
    *,
    name: str | None = None,
    description: str | None = "按日明细",
    fields: list[DataFieldInput] | None = None,
) -> DataTableReplace:
    return DataTableReplace(
        name=f"明细表-{uuid4().hex[:8]}" if name is None else name,
        description=description,
        fields=list(fixed_field_inputs() if fields is None else fields),
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
        source=DataImportSource.EXCEL,
        status=status,
        original_filename="day.xlsx",
        storage_path=f"imports/{uuid4().hex}.xlsx",
        content_sha256=uuid4().hex * 2,
        size_bytes=128,
        requested_by=owner_id,
    )
    session.add(job)
    await session.flush()
    return job


async def resolved(
    session: AsyncSession,
    table_id: int,
) -> ResolvedDataDefinition:
    definition = await data_table_repository.load_resolved_definition(
        session,
        table_id,
    )
    assert definition is not None
    return definition


def test_error_codes_are_exact() -> None:
    assert ErrorCode.DATA_TABLE_NOT_FOUND == 9001
    assert ErrorCode.DATA_TABLE_NAME_EXISTS == 9002
    assert ErrorCode.DATA_TABLE_LOCKED == 9003
    assert ErrorCode.DATA_TABLE_INVALID == 9004
    assert ErrorCode.DATA_IMPORT_NOT_FOUND == 9005
    assert ErrorCode.DATA_IMPORT_INVALID == 9006
    assert ErrorCode.DATA_IMPORT_VALIDATION_FAILED == 9007
    assert ErrorCode.DATA_IMPORT_IN_PROGRESS == 9008
    assert ErrorCode.DATA_QUERY_INVALID == 9009
    assert [int(code) for code in ErrorCode if 9001 <= int(code) <= 9009] == list(
        range(9001, 9010)
    )


async def test_create_table_persists_five_fixed_required_fields(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )

    assert isinstance(table, DataTableDefinition)
    assert table.status == DataTableStatus.DRAFT
    assert table.created_by == context.user_id
    assert table.locked_at is None
    definition = await resolved(session, table.id)
    assert len(definition.fields_by_system_key) == 5
    expected_types = {
        DataFieldRole.BUSINESS_DATE: DataFieldType.DATE,
        DataFieldRole.UPSTREAM_CUSTOMER: DataFieldType.CUSTOMER,
        DataFieldRole.DOWNSTREAM_CUSTOMER: DataFieldType.CUSTOMER,
        DataFieldRole.REVENUE: DataFieldType.DECIMAL,
        DataFieldRole.COST: DataFieldType.DECIMAL,
    }
    for role, field_type in expected_types.items():
        field = definition.fields_by_system_key[role]
        assert field.field_type == field_type
        assert field.required is True
    assert [field.label for field in definition.fields] == [
        "日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
    ]
    assert definition.custom_fields == ()


async def test_definition_inputs_are_trimmed(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    payload = make_table_payload(
        name="  季度明细  ",
        description="  按季度汇总  ",
        fields=[
            DataFieldInput(
                label="  业务日期  ",
                field_type=DataFieldType.DATE,
                system_key=DataFieldRole.BUSINESS_DATE,
                sort_order=0,
            ),
            *fixed_field_inputs()[1:],
            custom_field("  渠道备注  "),
        ],
    )

    table = await data_table_service.create_table(
        session,
        payload,
        context=context,
    )

    definition = await resolved(session, table.id)
    assert table.name == "季度明细"
    assert table.description == "按季度汇总"
    assert definition.labels == (
        "业务日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
        "渠道备注",
    )


async def test_blank_description_becomes_null(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(description="   "),
        context=context,
    )

    assert table.description is None


@pytest.mark.parametrize(
    "missing_role",
    [
        DataFieldRole.BUSINESS_DATE,
        DataFieldRole.UPSTREAM_CUSTOMER,
        DataFieldRole.DOWNSTREAM_CUSTOMER,
        DataFieldRole.REVENUE,
        DataFieldRole.COST,
    ],
)
async def test_missing_fixed_role_is_rejected(
    session: AsyncSession,
    context: AuthContext,
    missing_role: DataFieldRole,
) -> None:
    fields = [
        field for field in fixed_field_inputs() if field.system_key != missing_role
    ]

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fields),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID
    assert exc.value.http_status == 422


async def test_duplicate_fixed_role_is_rejected(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    fields = [
        *fixed_field_inputs(),
        DataFieldInput(
            label="另一个日期",
            field_type=DataFieldType.DATE,
            system_key=DataFieldRole.BUSINESS_DATE,
            sort_order=9,
        ),
    ]

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fields),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


@pytest.mark.parametrize(
    ("role", "wrong_type"),
    [
        (DataFieldRole.BUSINESS_DATE, DataFieldType.TEXT),
        (DataFieldRole.BUSINESS_DATE, DataFieldType.DECIMAL),
        (DataFieldRole.UPSTREAM_CUSTOMER, DataFieldType.TEXT),
        (DataFieldRole.DOWNSTREAM_CUSTOMER, DataFieldType.INTEGER),
        (DataFieldRole.REVENUE, DataFieldType.TEXT),
        (DataFieldRole.REVENUE, DataFieldType.INTEGER),
        (DataFieldRole.COST, DataFieldType.DATE),
    ],
)
async def test_fixed_role_requires_its_exact_type(
    session: AsyncSession,
    context: AuthContext,
    role: DataFieldRole,
    wrong_type: DataFieldType,
) -> None:
    fields = fixed_field_inputs()
    for field in fields:
        if field.system_key == role:
            field.field_type = wrong_type

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fields),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_fixed_fields_are_always_required_regardless_of_input(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    # 固定字段恒为必填：请求里省略或显式写 false 都不能让它们变可空。
    fields = [
        field.model_copy(update={"required": False}) for field in fixed_field_inputs()
    ]

    table = await data_table_service.create_table(
        session,
        make_table_payload(fields=fields),
        context=context,
    )

    definition = await resolved(session, table.id)
    assert all(field.required is True for field in definition.fields)
    assert len(definition.fields_by_system_key) == 5


async def test_custom_field_cannot_use_customer_type(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[
                    *fixed_field_inputs(),
                    custom_field("关联客户", field_type=DataFieldType.CUSTOMER),
                ],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_custom_required_flag_is_honored(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[
                *fixed_field_inputs(),
                custom_field("必填备注", required=True, sort_order=10),
                custom_field("可选备注", required=False, sort_order=11),
            ],
        ),
        context=context,
    )

    definition = await resolved(session, table.id)
    required_by_label = {
        field.label: field.required for field in definition.custom_fields
    }
    assert required_by_label == {"必填备注": True, "可选备注": False}


@pytest.mark.parametrize(
    "custom_label",
    ["日期", "上游客户", "收入", "渠道", " 渠道 ", "渠道 "],
)
async def test_duplicate_labels_are_rejected(
    session: AsyncSession,
    context: AuthContext,
    custom_label: str,
) -> None:
    fields = [
        *fixed_field_inputs(),
        custom_field("渠道", sort_order=10),
        custom_field(custom_label, sort_order=11),
    ]

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fields),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_label_uniqueness_is_case_insensitive(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    fields = [
        *fixed_field_inputs(),
        custom_field("Channel", sort_order=10),
        custom_field("CHANNEL", sort_order=11),
    ]

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fields),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


@pytest.mark.parametrize("label", ["", "   "])
async def test_blank_label_is_rejected(
    session: AsyncSession,
    context: AuthContext,
    label: str,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[*fixed_field_inputs(), custom_field(label)],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_too_long_label_is_rejected(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[*fixed_field_inputs(), custom_field("标" * 65)],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_enum_options_are_persisted_in_sort_order(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[
                *fixed_field_inputs(),
                enum_field(
                    "渠道",
                    options=[
                        DataEnumOptionInput(label=" 自然流量 ", sort_order=1),
                        DataEnumOptionInput(label="付费流量", sort_order=0),
                    ],
                ),
            ],
        ),
        context=context,
    )

    definition = await resolved(session, table.id)
    enum_field_row = definition.custom_fields[0]
    options = definition.options_for(enum_field_row.id)
    assert [option.label for option in options] == ["付费流量", "自然流量"]
    assert [option.sort_order for option in options] == [0, 1]
    assert all(option.id in definition.enum_options_by_id for option in options)
    assert set(definition.enum_options_by_field_id) == {enum_field_row.id}


async def test_options_on_non_enum_field_are_rejected(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[
                    *fixed_field_inputs(),
                    custom_field(
                        "备注",
                        options=[DataEnumOptionInput(label="A", sort_order=0)],
                    ),
                ],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_enum_field_requires_at_least_one_option(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[
                    *fixed_field_inputs(),
                    custom_field("渠道", field_type=DataFieldType.ENUM),
                ],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_enum_field_rejects_more_than_one_thousand_options(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    options = [
        DataEnumOptionInput(label=f"选项{index:04d}", sort_order=index)
        for index in range(1001)
    ]

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[*fixed_field_inputs(), enum_field("渠道", options=options)],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_enum_field_allows_exactly_one_thousand_options(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    options = [
        DataEnumOptionInput(label=f"选项{index:04d}", sort_order=index)
        for index in range(1000)
    ]

    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[*fixed_field_inputs(), enum_field("渠道", options=options)],
        ),
        context=context,
    )

    definition = await resolved(session, table.id)
    assert len(definition.options_for(definition.custom_fields[0].id)) == 1000


@pytest.mark.parametrize(
    "options",
    [
        [
            DataEnumOptionInput(label="A", sort_order=0),
            DataEnumOptionInput(label=" ", sort_order=1),
        ],
        [
            DataEnumOptionInput(label="A", sort_order=0),
            DataEnumOptionInput(label=" A ", sort_order=1),
        ],
        [
            DataEnumOptionInput(label="A", sort_order=0),
            DataEnumOptionInput(label="a", sort_order=1),
        ],
        [DataEnumOptionInput(label="标" * 101, sort_order=0)],
    ],
)
async def test_invalid_enum_options_are_rejected(
    session: AsyncSession,
    context: AuthContext,
    options: list[DataEnumOptionInput],
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(
                fields=[*fixed_field_inputs(), enum_field("渠道", options=options)],
            ),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_one_hundred_custom_fields_are_allowed(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    fields = [
        *fixed_field_inputs(),
        *(
            custom_field(f"自定义{index:03d}", sort_order=100 + index)
            for index in range(100)
        ),
    ]

    table = await data_table_service.create_table(
        session,
        make_table_payload(fields=fields),
        context=context,
    )

    definition = await resolved(session, table.id)
    assert len(definition.custom_fields) == 100


async def test_more_than_one_hundred_custom_fields_are_rejected(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    fields = [
        *fixed_field_inputs(),
        *(
            custom_field(f"自定义{index:03d}", sort_order=100 + index)
            for index in range(101)
        ),
    ]

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fields),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


@pytest.mark.parametrize("name", ["", "   "])
async def test_blank_table_name_is_rejected(
    session: AsyncSession,
    context: AuthContext,
    name: str,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(name=name),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_too_long_table_name_is_rejected(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(name="表" * 101),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


async def test_too_long_description_is_rejected(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(description="描" * 501),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_INVALID


@pytest.mark.parametrize("variant", ["同名表", " 同名表 ", "同名表 "])
async def test_duplicate_table_name_returns_9002(
    session: AsyncSession,
    context: AuthContext,
    variant: str,
) -> None:
    await data_table_service.create_table(
        session,
        make_table_payload(name="同名表"),
        context=context,
    )

    with pytest.raises(BusinessError) as exc:
        await data_table_service.create_table(
            session,
            make_table_payload(name=variant),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_NAME_EXISTS
    assert exc.value.http_status == 409


async def test_rename_to_existing_table_name_returns_9002(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    await data_table_service.create_table(
        session,
        make_table_payload(name="第一张表"),
        context=context,
    )
    second = await data_table_service.create_table(
        session,
        make_table_payload(name="第二张表"),
        context=context,
    )

    with pytest.raises(BusinessError) as exc:
        await data_table_service.replace_table(
            session,
            second.id,
            make_table_payload(name="第一张表"),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_NAME_EXISTS
    reloaded = await session.get(DataTableDefinition, second.id)
    assert reloaded is not None
    assert reloaded.name == "第二张表"
    definition = await resolved(session, second.id)
    assert len(definition.fields_by_system_key) == 5


async def test_replace_fully_recreates_draft_definition(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[
                *fixed_field_inputs(),
                custom_field("旧字段", sort_order=10),
                enum_field(
                    "旧枚举",
                    sort_order=11,
                    options=[DataEnumOptionInput(label="旧选项", sort_order=0)],
                ),
            ],
        ),
        context=context,
    )
    before = await resolved(session, table.id)
    old_field_ids = set(before.fields_by_id)
    old_option_ids = set(before.enum_options_by_id)

    replaced = await data_table_service.replace_table(
        session,
        table.id,
        make_table_payload(
            name="替换后的名称",
            description="替换后的描述",
            fields=[
                DataFieldInput(
                    label="业务日期",
                    field_type=DataFieldType.DATE,
                    system_key=DataFieldRole.BUSINESS_DATE,
                    sort_order=0,
                ),
                *fixed_field_inputs()[1:],
                custom_field("新字段", required=True, sort_order=20),
                enum_field(
                    "新枚举",
                    sort_order=21,
                    options=[
                        DataEnumOptionInput(label="新选项一", sort_order=0),
                        DataEnumOptionInput(label="新选项二", sort_order=1),
                    ],
                ),
            ],
        ),
        context=context,
    )

    after = await resolved(session, table.id)
    assert replaced.id == table.id
    assert after.table.name == "替换后的名称"
    assert after.table.description == "替换后的描述"
    assert after.table.status == DataTableStatus.DRAFT
    assert set(after.fields_by_id).isdisjoint(old_field_ids)
    assert set(after.enum_options_by_id).isdisjoint(old_option_ids)
    assert [field.label for field in after.custom_fields] == ["新字段", "新枚举"]
    custom = after.custom_fields[0]
    assert custom.required is True
    enum_row = after.custom_fields[1]
    assert [option.label for option in after.options_for(enum_row.id)] == [
        "新选项一",
        "新选项二",
    ]
    assert len(after.fields_by_system_key) == 5


async def test_locked_table_rejects_any_definition_change(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    table.status = DataTableStatus.LOCKED
    table.locked_at = datetime.now(UTC)
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await data_table_service.replace_table(
            session,
            table.id,
            make_table_payload(name="新名称"),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_LOCKED
    assert exc.value.http_status == 409


async def test_locked_table_cannot_be_deleted(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    table.status = DataTableStatus.LOCKED
    table.locked_at = datetime.now(UTC)
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await data_table_service.delete_table(
            session,
            table.id,
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_LOCKED
    assert await session.get(DataTableDefinition, table.id) is not None


@pytest.mark.parametrize(
    "status",
    [
        DataImportStatus.QUEUED,
        DataImportStatus.VALIDATING,
        DataImportStatus.APPLYING,
    ],
)
async def test_unfinished_import_blocks_definition_change(
    session: AsyncSession,
    context: AuthContext,
    status: DataImportStatus,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    await add_import(
        session,
        table,
        status=status,
        owner_id=context.user_id,
    )

    with pytest.raises(BusinessError) as exc:
        await data_table_service.replace_table(
            session,
            table.id,
            make_table_payload(name="导入中改名"),
            context=context,
        )
    assert exc.value.code == ErrorCode.DATA_IMPORT_IN_PROGRESS
    assert exc.value.http_status == 409

    with pytest.raises(BusinessError) as exc:
        await data_table_service.delete_table(
            session,
            table.id,
            context=context,
        )
    assert exc.value.code == ErrorCode.DATA_IMPORT_IN_PROGRESS
    assert await session.get(DataTableDefinition, table.id) is not None


@pytest.mark.parametrize(
    "status",
    [DataImportStatus.SUCCEEDED, DataImportStatus.FAILED],
)
async def test_terminal_import_does_not_block_definition_change(
    session: AsyncSession,
    context: AuthContext,
    status: DataImportStatus,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    await add_import(
        session,
        table,
        status=status,
        owner_id=context.user_id,
    )

    replaced = await data_table_service.replace_table(
        session,
        table.id,
        make_table_payload(name="终态后改名"),
        context=context,
    )

    assert replaced.name == "终态后改名"


async def test_replace_keeps_failed_import_diagnostics_but_clears_field_ids(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[*fixed_field_inputs(), custom_field("备注", sort_order=10)],
        ),
        context=context,
    )
    definition = await resolved(session, table.id)
    custom = definition.custom_fields[0]
    job = await add_import(
        session,
        table,
        status=DataImportStatus.FAILED,
        owner_id=context.user_id,
    )
    error = DataImportError(
        import_id=job.id,
        sheet_name="Sheet1",
        row_number=2,
        column_label="备注",
        field_id=custom.id,
        error_code="TEXT_TOO_LONG",
        message="文本超出上限",
    )
    session.add(error)
    await session.flush()

    await data_table_service.replace_table(
        session,
        table.id,
        make_table_payload(
            fields=[*fixed_field_inputs(), custom_field("新备注", sort_order=10)],
        ),
        context=context,
    )

    refreshed = await session.get(DataImportError, error.id)
    assert refreshed is not None
    assert refreshed.field_id is None
    assert refreshed.column_label == "备注"
    assert refreshed.message == "文本超出上限"


async def test_delete_physically_removes_draft_table_and_history(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[
                *fixed_field_inputs(),
                enum_field(
                    "渠道",
                    options=[DataEnumOptionInput(label="A", sort_order=0)],
                ),
            ],
        ),
        context=context,
    )
    definition = await resolved(session, table.id)
    enum_row = definition.custom_fields[0]
    option = definition.options_for(enum_row.id)[0]
    job = await add_import(
        session,
        table,
        status=DataImportStatus.FAILED,
        owner_id=context.user_id,
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
        upstream_customer_name_source="上游来源名称",
        downstream_customer_name_source="下游来源名称",
        revenue=Decimal("1942.51000000"),
        cost=Decimal("1155.14000000"),
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
        column_label="渠道",
        field_id=enum_row.id,
        error_code="ENUM_UNKNOWN",
        message="枚举值不在选项中",
    )
    session.add(error)
    await session.flush()

    await data_table_service.delete_table(
        session,
        table.id,
        context=context,
    )

    assert await session.get(DataTableDefinition, table.id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataFieldDefinition)
            .where(DataFieldDefinition.table_id == table.id)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataEnumOption)
            .where(DataEnumOption.field_id == enum_row.id)
        )
        == 0
    )
    assert await session.get(DataImportJob, job.id) is None
    assert await session.get(DataSnapshot, snapshot.id) is None
    assert await session.get(DataRecord, record.id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataRecordValue)
            .where(DataRecordValue.record_id == record.id)
        )
        == 0
    )
    assert await session.get(DataImportError, error.id) is None


async def test_delete_removes_current_snapshot_pointers(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    job = await add_import(
        session,
        table,
        status=DataImportStatus.FAILED,
        owner_id=context.user_id,
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
    await session.execute(
        DataCurrentSnapshot.insert().values(
            table_id=table.id,
            business_date=date(2026, 10, 18),
            snapshot_id=snapshot.id,
        )
    )

    await data_table_service.delete_table(
        session,
        table.id,
        context=context,
    )

    remaining = await session.scalar(
        select(func.count())
        .select_from(DataCurrentSnapshot)
        .where(DataCurrentSnapshot.c.table_id == table.id)
    )
    assert remaining == 0


async def test_delete_missing_table_returns_9001(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.delete_table(session, 0, context=context)

    assert exc.value.code == ErrorCode.DATA_TABLE_NOT_FOUND
    assert exc.value.http_status == 404


async def test_get_missing_table_returns_9001(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.get_table(session, 0, context=context)

    assert exc.value.code == ErrorCode.DATA_TABLE_NOT_FOUND


async def test_replace_missing_table_returns_9001(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    with pytest.raises(BusinessError) as exc:
        await data_table_service.replace_table(
            session,
            0,
            make_table_payload(),
            context=context,
        )

    assert exc.value.code == ErrorCode.DATA_TABLE_NOT_FOUND


async def test_get_table_returns_resolved_definition(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(fields=[*fixed_field_inputs(), custom_field("备注")]),
        context=context,
    )

    definition = await data_table_service.get_table(
        session,
        table.id,
        context=context,
    )

    assert isinstance(definition, ResolvedDataDefinition)
    assert definition.table.id == table.id
    assert definition.labels == (
        "日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
        "备注",
    )


async def test_list_tables_returns_every_definition(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    first = await data_table_service.create_table(
        session,
        make_table_payload(name="列表甲"),
        context=context,
    )
    second = await data_table_service.create_table(
        session,
        make_table_payload(name="列表乙"),
        context=context,
    )

    definitions = await data_table_service.list_tables(session, context=context)

    by_id = {definition.table.id: definition for definition in definitions}
    assert {first.id, second.id} <= set(by_id)
    assert [definition.table.id for definition in definitions] == sorted(by_id)
    assert len(by_id[first.id].fields_by_system_key) == 5


async def test_definition_read_model_exposes_fields_and_options(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(
            fields=[
                *fixed_field_inputs(),
                enum_field(
                    "渠道",
                    options=[DataEnumOptionInput(label="A", sort_order=0)],
                ),
            ],
        ),
        context=context,
    )
    definition = await data_table_service.get_table(
        session,
        table.id,
        context=context,
    )

    read = DataTableDefinitionRead.from_resolved(definition)

    assert read.id == table.id
    assert read.status == DataTableStatus.DRAFT
    assert [field.label for field in read.fields][:5] == [
        "日期",
        "上游客户",
        "下游客户",
        "收入",
        "成本",
    ]
    enum_read = read.fields[-1]
    assert enum_read.field_type == DataFieldType.ENUM
    assert [option.label for option in enum_read.options] == ["A"]
    payload = read.model_dump(mode="json")
    assert payload["created_at"].endswith("+00:00")
    assert payload["fields"][0]["field_type"] == "DATE"
    assert payload["fields"][0]["options"] == []


async def test_write_services_commit_and_reads_do_not(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits: list[int] = []
    original_commit = session.commit

    async def counting_commit() -> None:
        commits.append(1)
        await original_commit()

    monkeypatch.setattr(session, "commit", counting_commit)

    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    assert len(commits) == 1

    await data_table_service.replace_table(
        session,
        table.id,
        make_table_payload(name="改名"),
        context=context,
    )
    assert len(commits) == 2

    await data_table_service.get_table(session, table.id, context=context)
    await data_table_service.list_tables(session, context=context)
    assert len(commits) == 2

    await data_table_service.delete_table(
        session,
        table.id,
        context=context,
    )
    assert len(commits) == 3


async def test_definition_mutation_locks_table_before_import_check(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    events: list[str] = []
    original_lock = data_table_repository.get_table_for_update
    original_check = data_table_repository.has_unfinished_import

    async def recording_lock(
        lock_session: AsyncSession,
        table_id: int,
    ) -> DataTableDefinition | None:
        events.append("lock")
        return await original_lock(lock_session, table_id)

    async def recording_check(
        check_session: AsyncSession,
        table_id: int,
    ) -> bool:
        events.append("imports")
        return await original_check(check_session, table_id)

    monkeypatch.setattr(
        data_table_repository,
        "get_table_for_update",
        recording_lock,
    )
    monkeypatch.setattr(
        data_table_repository,
        "has_unfinished_import",
        recording_check,
    )

    await data_table_service.replace_table(
        session,
        table.id,
        make_table_payload(name="锁序检查"),
        context=context,
    )
    await data_table_service.delete_table(
        session,
        table.id,
        context=context,
    )

    assert events == ["lock", "imports", "lock", "imports"]


async def test_create_writes_success_audit_entry(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(audit_service, "record_audit_entry", record)

    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    table_id = table.id
    await data_table_service.replace_table(
        session,
        table_id,
        make_table_payload(name="审计改名"),
        context=context,
    )
    await data_table_service.delete_table(
        session,
        table_id,
        context=context,
    )

    assert [entry["action"] for entry in recorded] == [
        "data_table:create",
        "data_table:update",
        "data_table:delete",
    ]
    assert [entry["resource_type"] for entry in recorded] == [
        "custom_data_table",
        "custom_data_table",
        "custom_data_table",
    ]
    assert [entry["resource_id"] for entry in recorded] == [
        table_id,
        table_id,
        table_id,
    ]
    assert all(
        entry["actor"] is not None and entry["actor"].user_id == context.user_id
        for entry in recorded
    )


async def test_invalid_definition_is_not_audited(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(audit_service, "record_audit_entry", record)

    with pytest.raises(BusinessError):
        await data_table_service.create_table(
            session,
            make_table_payload(fields=fixed_field_inputs()[:-1]),
            context=context,
        )

    assert recorded == []


async def test_create_does_not_leak_definition_rows_on_duplicate_name(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    await data_table_service.create_table(
        session,
        make_table_payload(name="事务回滚表"),
        context=context,
    )

    with pytest.raises(BusinessError):
        await data_table_service.create_table(
            session,
            make_table_payload(name="事务回滚表"),
            context=context,
        )

    assert (
        await session.scalar(
            select(func.count())
            .select_from(DataTableDefinition)
            .where(DataTableDefinition.name == "事务回滚表")
        )
        == 1
    )
    orphan_fields = await session.scalar(
        select(func.count())
        .select_from(DataFieldDefinition)
        .where(DataFieldDefinition.table_id.not_in(select(DataTableDefinition.id)))
    )
    assert orphan_fields == 0


async def test_create_accepts_data_table_create_payload(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        DataTableCreate(
            name=f"创建基类-{uuid4().hex[:8]}",
            description=None,
            fields=fixed_field_inputs(),
        ),
        context=context,
    )

    assert table.id > 0
    assert table.description is None


async def test_replace_accepts_data_table_create_payload(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    """替换与创建共用同一字段集合契约，避免两套校验。"""
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    payload = DataTableCreate(
        name=f"创建式替换-{uuid4().hex[:8]}",
        description=None,
        fields=fixed_field_inputs(),
    )

    replaced = await data_table_service.replace_table(
        session,
        table.id,
        payload,
        context=context,
    )

    assert replaced.name == payload.name


async def test_replace_accepts_data_table_replace_payload(
    session: AsyncSession,
    context: AuthContext,
) -> None:
    table = await data_table_service.create_table(
        session,
        make_table_payload(),
        context=context,
    )
    payload = DataTableReplace(
        name=f"替换基类-{uuid4().hex[:8]}",
        description="替换",
        fields=fixed_field_inputs(),
    )

    replaced = await data_table_service.replace_table(
        session,
        table.id,
        payload,
        context=context,
    )

    assert replaced.name == payload.name
