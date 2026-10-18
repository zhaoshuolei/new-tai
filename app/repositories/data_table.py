"""可配置数据表定义的数据访问。

服务层负责业务校验；本模块只负责持久化、加锁顺序、唯一冲突映射与
删除依赖顺序。写入只做 flush，事务边界由服务层控制。

加锁约定：任何定义变更或删除都必须先取得 ``custom_data_table`` 行锁
（``get_table_for_update``），再检查未完成导入；导入入队同样先锁该行，
因此“检查—写入”之间不会插入新任务（见设计 6.4、8.1）。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportError,
    DataImportJob,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
    DataTableStatus,
)

_UNFINISHED_IMPORT_STATUSES: tuple[DataImportStatus, ...] = (
    DataImportStatus.QUEUED,
    DataImportStatus.VALIDATING,
    DataImportStatus.APPLYING,
)

_NAME_UNIQUE_INDEX = "uq_custom_data_table_name"
_MYSQL_DUPLICATE_ENTRY = 1062

# 字段/选项唯一索引对应的业务错误：标签重复属于定义不合法（9004）。
_DEFINITION_UNIQUE_INDEXES: tuple[tuple[str, str], ...] = (
    ("uq_custom_data_field_table_label", "Field label already exists in this table"),
    (
        "uq_custom_data_field_table_system_key",
        "System field is duplicated in this table",
    ),
    (
        "uq_custom_data_enum_option_field_label",
        "Enum option label already exists in this field",
    ),
)


@dataclass(frozen=True)
class EnumOptionSpec:
    """一个校验通过、尚未落库的枚举选项。"""

    label: str
    sort_order: int = 0


@dataclass(frozen=True)
class FieldSpec:
    """一个校验通过、尚未落库的字段定义。

    字段 ID 只在数据库生成；DRAFT 阶段整体替换时 ID 可以变化，
    因为此时不可能存在引用它的成功数据（见设计 8.1）。
    """

    label: str
    field_type: DataFieldType
    system_key: DataFieldRole
    required: bool = False
    sort_order: int = 0
    options: tuple[EnumOptionSpec, ...] = ()


@dataclass(frozen=True)
class ResolvedDataDefinition:
    """一次性解析的表定义，供导入、模板与查询复用。

    ``fields_by_system_key`` 只包含五个固定角色；自定义字段通过
    ``custom_fields`` 或 ``fields_by_id`` 访问。
    """

    table: DataTableDefinition
    fields: tuple[DataFieldDefinition, ...]
    fields_by_id: dict[int, DataFieldDefinition]
    fields_by_system_key: dict[DataFieldRole, DataFieldDefinition]
    enum_options_by_id: dict[int, DataEnumOption]
    enum_options_by_field_id: dict[int, tuple[DataEnumOption, ...]] = field(
        default_factory=dict,
    )

    @property
    def labels(self) -> tuple[str, ...]:
        """按展示顺序返回全部字段标签，用于 Excel 表头与模板。"""
        return tuple(item.label for item in self.fields)

    @property
    def custom_fields(self) -> tuple[DataFieldDefinition, ...]:
        return tuple(
            item for item in self.fields if item.system_key == DataFieldRole.CUSTOM
        )

    def options_for(self, field_id: int) -> tuple[DataEnumOption, ...]:
        return self.enum_options_by_field_id.get(field_id, ())

    def field_for(self, system_key: DataFieldRole) -> DataFieldDefinition | None:
        return self.fields_by_system_key.get(system_key)


def name_exists_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_NAME_EXISTS,
        "Data table name already exists",
        http_status=409,
    )


def table_locked_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_LOCKED,
        "Data table is locked",
        http_status=409,
    )


def _duplicate_index(error: IntegrityError) -> str | None:
    """返回触发唯一冲突的索引名；非 1062 或无法识别时返回 None。"""
    arguments = getattr(error.orig, "args", ())
    if not arguments:
        return None
    if int(arguments[0]) != _MYSQL_DUPLICATE_ENTRY:
        return None
    message = str(arguments[1])
    for index_name in (
        _NAME_UNIQUE_INDEX,
        *(name for name, _ in _DEFINITION_UNIQUE_INDEXES),
    ):
        if index_name in message:
            return index_name
    return None


def _is_duplicate_name(error: IntegrityError) -> bool:
    return _duplicate_index(error) == _NAME_UNIQUE_INDEX


def _translate_definition_conflict(error: IntegrityError) -> BusinessError | None:
    """把字段/选项唯一索引冲突翻译成 9004。

    进程内校验已经覆盖了大小写与首尾空白，但数据库排序规则
    （``utf8mb4_unicode_ci``）还会把重音变体视为相同，因此仍需兜底。
    """
    index_name = _duplicate_index(error)
    for candidate, message in _DEFINITION_UNIQUE_INDEXES:
        if candidate == index_name:
            return BusinessError(
                ErrorCode.DATA_TABLE_INVALID,
                message,
                http_status=422,
            )
    return None


async def get_table(
    session: AsyncSession,
    table_id: int,
) -> DataTableDefinition | None:
    return await session.get(DataTableDefinition, table_id)


def build_table_for_update_statement(
    table_id: int,
) -> Select[DataTableDefinition]:
    """构造定义变更前的当前读加锁语句（便于单独断言 SQL 形状）。"""
    return (
        select(DataTableDefinition)
        .where(DataTableDefinition.id == table_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_table_for_update(
    session: AsyncSession,
    table_id: int,
) -> DataTableDefinition | None:
    """加锁读取表定义。

    MySQL 默认 REPEATABLE READ 下普通 SELECT 会固定事务快照，看不到并发
    事务刚提交的状态；``FOR UPDATE`` 是当前读，也正好让定义变更与导入
    入队在同一行上串行化。
    """
    return await session.scalar(build_table_for_update_statement(table_id))


async def list_tables(session: AsyncSession) -> list[DataTableDefinition]:
    statement = select(DataTableDefinition).order_by(DataTableDefinition.id)
    return list((await session.scalars(statement)).all())


async def has_unfinished_import(session: AsyncSession, table_id: int) -> bool:
    """判断表上是否存在排队、校验或应用中的导入任务。

    使用 ``FOR UPDATE`` 强制当前读：调用方虽然已持有表行锁，但事务快照
    可能建立于并发入队提交之前，普通 SELECT 会漏掉刚提交的任务。

    锁顺序约定：调用方必须先锁 ``custom_data_table`` 行，再调用本函数；
    worker 认领/激活导入时同样先锁表行。所有路径保持“表行 → 导入行”的
    单一顺序，才不会与定义变更互相死锁。
    """
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


async def load_resolved_definitions(
    session: AsyncSession,
    table_ids: Sequence[int] | None = None,
) -> list[ResolvedDataDefinition]:
    """批量解析表定义：表、字段、选项各一次查询，避免逐表 N+1。"""
    table_statement = select(DataTableDefinition).order_by(DataTableDefinition.id)
    if table_ids is not None:
        if not table_ids:
            return []
        table_statement = table_statement.where(
            DataTableDefinition.id.in_(list(table_ids))
        )
    tables = list((await session.scalars(table_statement)).all())
    if not tables:
        return []

    selected_ids = [table.id for table in tables]
    field_statement = (
        select(DataFieldDefinition)
        .where(DataFieldDefinition.table_id.in_(selected_ids))
        .order_by(
            DataFieldDefinition.table_id,
            DataFieldDefinition.sort_order,
            DataFieldDefinition.id,
        )
    )
    fields_by_table_id: dict[int, list[DataFieldDefinition]] = {}
    for item in (await session.scalars(field_statement)).all():
        fields_by_table_id.setdefault(item.table_id, []).append(item)

    field_ids = [item.id for fields in fields_by_table_id.values() for item in fields]
    options_by_field_id: dict[int, list[DataEnumOption]] = {}
    if field_ids:
        option_statement = (
            select(DataEnumOption)
            .where(DataEnumOption.field_id.in_(field_ids))
            .order_by(
                DataEnumOption.field_id,
                DataEnumOption.sort_order,
                DataEnumOption.id,
            )
        )
        for option in (await session.scalars(option_statement)).all():
            options_by_field_id.setdefault(option.field_id, []).append(option)

    definitions: list[ResolvedDataDefinition] = []
    for table in tables:
        fields = tuple(fields_by_table_id.get(table.id, ()))
        field_options = {
            item.id: tuple(options)
            for item in fields
            if (options := options_by_field_id.get(item.id))
        }
        definitions.append(
            ResolvedDataDefinition(
                table=table,
                fields=fields,
                fields_by_id={item.id: item for item in fields},
                fields_by_system_key={
                    item.system_key: item
                    for item in fields
                    if item.system_key != DataFieldRole.CUSTOM
                },
                enum_options_by_id={
                    option.id: option
                    for options in field_options.values()
                    for option in options
                },
                enum_options_by_field_id=field_options,
            )
        )
    return definitions


async def load_resolved_definition(
    session: AsyncSession,
    table_id: int,
) -> ResolvedDataDefinition | None:
    """解析单张表的定义；表不存在时返回 None。"""
    definitions = await load_resolved_definitions(session, [table_id])
    return definitions[0] if definitions else None


async def _reload_definition_rows(
    session: AsyncSession,
    table_id: int,
) -> None:
    """保存点回滚后重新加载对象状态，避免后续异步惰性加载。"""
    await session.execute(
        select(DataTableDefinition)
        .where(DataTableDefinition.id == table_id)
        .execution_options(populate_existing=True)
    )
    field_ids = list(
        (
            await session.scalars(
                select(DataFieldDefinition.id)
                .where(DataFieldDefinition.table_id == table_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if field_ids:
        await session.execute(
            select(DataEnumOption)
            .where(DataEnumOption.field_id.in_(field_ids))
            .execution_options(populate_existing=True)
        )


async def _add_field_rows(
    session: AsyncSession,
    table_id: int,
    field_specs: list[FieldSpec],
) -> None:
    """写入字段行，并按输入顺序补充枚举选项。"""
    rows = [
        DataFieldDefinition(
            table_id=table_id,
            label=spec.label,
            field_type=spec.field_type,
            system_key=spec.system_key,
            required=spec.required,
            sort_order=spec.sort_order,
        )
        for spec in field_specs
    ]
    session.add_all(rows)
    await session.flush()

    options = [
        DataEnumOption(
            field_id=row.id,
            label=option.label,
            sort_order=option.sort_order,
        )
        for row, spec in zip(rows, field_specs, strict=True)
        for option in spec.options
    ]
    if options:
        session.add_all(options)
        await session.flush()


async def create_table(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    fields: list[FieldSpec],
    created_by: int,
) -> DataTableDefinition:
    """写入完整 DRAFT 定义；表名冲突在保存点内翻译为 9002。"""
    table = DataTableDefinition(
        name=name,
        description=description,
        status=DataTableStatus.DRAFT,
        created_by=created_by,
        updated_by=None,
    )
    try:
        async with session.begin_nested():
            session.add(table)
            await session.flush()
            await _add_field_rows(session, table.id, fields)
    except IntegrityError as error:
        if _is_duplicate_name(error):
            raise name_exists_error() from error
        invalid = _translate_definition_conflict(error)
        if invalid is not None:
            raise invalid from error
        raise
    await session.refresh(table)
    return table


def _table_import_ids(table_id: int) -> Select[int]:
    return select(DataImportJob.id).where(DataImportJob.table_id == table_id)


async def _clear_import_error_field_references(
    session: AsyncSession,
    table_id: int,
) -> None:
    """保留失败导入的诊断信息，只解除即将删除的字段引用。"""
    await session.execute(
        update(DataImportError)
        .where(
            DataImportError.import_id.in_(_table_import_ids(table_id)),
            DataImportError.field_id.is_not(None),
        )
        .values(field_id=None)
    )


async def _delete_record_values(
    session: AsyncSession,
    record_ids: Select[int] | list[int],
) -> None:
    """删除记录值行：它们对字段与枚举选项都是 RESTRICT。"""
    await session.execute(
        delete(DataRecordValue).where(DataRecordValue.record_id.in_(record_ids))
    )


async def _delete_field_rows(session: AsyncSession, table_id: int) -> None:
    """按“选项 → 字段”删除定义行；此时不应再有值行引用它们。"""
    field_ids = select(DataFieldDefinition.id).where(
        DataFieldDefinition.table_id == table_id
    )
    await session.execute(
        delete(DataEnumOption).where(DataEnumOption.field_id.in_(field_ids))
    )
    await session.execute(
        delete(DataFieldDefinition).where(DataFieldDefinition.table_id == table_id)
    )
    await session.flush()


async def _purge_staged_snapshot_rows(
    session: AsyncSession,
    table_id: int,
) -> None:
    """清理遗留的 STAGED 快照及其记录。

    DRAFT 阶段不可能存在成功生效的快照（成功导入会把表置为 LOCKED），
    但 worker 崩溃可能留下 STAGED 数据；它们引用即将被替换的字段与选项，
    受 RESTRICT 保护，必须先清理才能重建定义。
    """
    snapshot_ids = list(
        (
            await session.scalars(
                select(DataSnapshot.id).where(
                    DataSnapshot.table_id == table_id,
                    DataSnapshot.status == DataSnapshotStatus.STAGED,
                )
            )
        ).all()
    )
    if not snapshot_ids:
        return

    # MySQL 不允许在 DELETE 的子查询里引用同一目标表（错误 1093），
    # 因此先取出主键，再用显式 ID 列表删除。
    record_ids = list(
        (
            await session.scalars(
                select(DataRecord.id).where(DataRecord.snapshot_id.in_(snapshot_ids))
            )
        ).all()
    )
    if record_ids:
        await _delete_record_values(session, record_ids)
        await session.execute(delete(DataRecord).where(DataRecord.id.in_(record_ids)))
    await session.execute(
        DataCurrentSnapshot.delete().where(
            DataCurrentSnapshot.c.snapshot_id.in_(snapshot_ids)
        )
    )
    await session.execute(delete(DataSnapshot).where(DataSnapshot.id.in_(snapshot_ids)))


async def replace_table(
    session: AsyncSession,
    table: DataTableDefinition,
    *,
    name: str,
    description: str | None,
    fields: list[FieldSpec],
    updated_by: int | None,
) -> DataTableDefinition:
    """整体替换 DRAFT 定义的字段与选项，字段 ID 会随之变化。

    调用方必须先锁表行并确认没有未完成导入（见 ``has_unfinished_import``）。
    旧字段与旧选项被物理删除，因此这里会先清掉遗留的 STAGED 快照；
    ACTIVE/SUPERSEDED 快照不可能出现在 DRAFT 表上——成功激活会在同一
    事务内把表置为 LOCKED，所以此处不做静默的数据删除。
    """
    if table.status != DataTableStatus.DRAFT:
        raise table_locked_error()

    table_id = table.id
    try:
        async with session.begin_nested():
            table.name = name
            table.description = description
            table.updated_by = updated_by
            await session.flush()
            await _purge_staged_snapshot_rows(session, table_id)
            await _clear_import_error_field_references(session, table_id)
            await _delete_field_rows(session, table_id)
            await _add_field_rows(session, table_id, fields)
    except IntegrityError as error:
        if _is_duplicate_name(error):
            await _reload_definition_rows(session, table_id)
            raise name_exists_error() from error
        invalid = _translate_definition_conflict(error)
        if invalid is not None:
            await _reload_definition_rows(session, table_id)
            raise invalid from error
        raise
    await session.refresh(table)
    return table


async def _delete_import_rows(session: AsyncSession, table_id: int) -> None:
    await session.execute(
        delete(DataImportError).where(
            DataImportError.import_id.in_(_table_import_ids(table_id))
        )
    )
    await session.execute(
        delete(DataImportJob).where(DataImportJob.table_id == table_id)
    )


async def _delete_snapshot_rows(session: AsyncSession, table_id: int) -> None:
    """删除全部快照、记录与当前指针。

    ``custom_data_current_snapshot.snapshot_id`` 是 RESTRICT，必须先删指针；
    记录值对字段/选项是 RESTRICT，所以显式先删值行再删字段。
    """
    record_ids = select(DataRecord.id).where(DataRecord.table_id == table_id)
    await _delete_record_values(session, record_ids)
    await session.execute(delete(DataRecord).where(DataRecord.table_id == table_id))
    await session.execute(
        DataCurrentSnapshot.delete().where(DataCurrentSnapshot.c.table_id == table_id)
    )
    await session.execute(delete(DataSnapshot).where(DataSnapshot.table_id == table_id))


async def delete_table(
    session: AsyncSession,
    table: DataTableDefinition,
) -> None:
    """物理删除 DRAFT 表及其失败导入、快照和记录。"""
    if table.status != DataTableStatus.DRAFT:
        raise table_locked_error()

    table_id = table.id
    await _delete_snapshot_rows(session, table_id)
    await _delete_import_rows(session, table_id)
    await _delete_field_rows(session, table_id)
    await session.execute(
        delete(DataTableDefinition).where(DataTableDefinition.id == table_id)
    )
    await session.flush()


__all__: tuple[str, ...] = (
    "EnumOptionSpec",
    "FieldSpec",
    "ResolvedDataDefinition",
    "build_table_for_update_statement",
    "create_table",
    "delete_table",
    "get_table",
    "get_table_for_update",
    "has_unfinished_import",
    "list_tables",
    "load_resolved_definition",
    "load_resolved_definitions",
    "name_exists_error",
    "replace_table",
    "table_locked_error",
)
