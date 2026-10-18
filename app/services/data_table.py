"""可配置数据表定义的业务规则与生命周期。

本模块负责表名与字段定义的规范化、业务校验以及 DRAFT 生命周期。
持久化细节、加锁顺序与唯一冲突映射在 ``app.repositories.data_table``。
写入服务自行提交；读取服务不提交。

冻结语义见设计 8.2：一旦表状态为 LOCKED，任何元数据或字段修改都返回
9003；存在未完成导入时任何修改或删除都返回 9008。
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.data_management import (
    DataFieldRole,
    DataFieldType,
    DataTableDefinition,
    DataTableStatus,
)
from app.repositories import data_table as data_table_repository
from app.repositories.data_table import (
    EnumOptionSpec,
    FieldSpec,
    ResolvedDataDefinition,
)
from app.schemas.data_table import DataFieldInput, DataTableCreate
from app.services.audit import audited

_NAME_MAX_LENGTH = 100
_DESCRIPTION_MAX_LENGTH = 500
_LABEL_MAX_LENGTH = 64
_OPTION_LABEL_MAX_LENGTH = 100
_MAX_CUSTOM_FIELDS = 100
_MAX_ENUM_OPTIONS = 1000

_FIXED_ROLE_TYPES: dict[DataFieldRole, DataFieldType] = {
    DataFieldRole.BUSINESS_DATE: DataFieldType.DATE,
    DataFieldRole.UPSTREAM_CUSTOMER: DataFieldType.CUSTOMER,
    DataFieldRole.DOWNSTREAM_CUSTOMER: DataFieldType.CUSTOMER,
    DataFieldRole.REVENUE: DataFieldType.DECIMAL,
    DataFieldRole.COST: DataFieldType.DECIMAL,
}

_CUSTOM_FIELD_TYPES: frozenset[DataFieldType] = frozenset(
    {
        DataFieldType.TEXT,
        DataFieldType.INTEGER,
        DataFieldType.DECIMAL,
        DataFieldType.DATE,
        DataFieldType.BOOLEAN,
        DataFieldType.ENUM,
    }
)


def invalid_definition_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_INVALID,
        message,
        http_status=422,
    )


def _not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_NOT_FOUND,
        "Data table not found",
        http_status=404,
    )


def _locked_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_LOCKED,
        "Data table is locked and cannot be modified",
        http_status=409,
    )


def _import_in_progress_error() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_IN_PROGRESS,
        "Data table has an unfinished import",
        http_status=409,
    )


def _data_exists_error() -> BusinessError:
    """已经有成功数据时的定义保护错误。

    错误码复用 9003：设计 8.2 把“成功生效后不可修改”定义为冻结语义，
    9003 的对外含义正是“数据表已冻结，不能修改”。正常流程下成功数据
    必然伴随 LOCKED，因此不需要新错误码；这里只为恢复残留或状态缺陷
    补一条防御性拒绝，语义与冻结完全一致。
    """
    return BusinessError(
        ErrorCode.DATA_TABLE_LOCKED,
        "Data table already contains successful data and is locked",
        http_status=409,
    )


def _normalize_label(label: str, *, kind: str) -> str:
    value = label.strip()
    if not value:
        raise invalid_definition_error(f"{kind} label is required")
    return value


def _normalize_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise invalid_definition_error("Data table name is required")
    if len(value) > _NAME_MAX_LENGTH:
        raise invalid_definition_error(
            f"Data table name must not exceed {_NAME_MAX_LENGTH} characters"
        )
    return value


def _normalize_description(description: str | None) -> str | None:
    if description is None:
        return None
    value = description.strip()
    if not value:
        return None
    if len(value) > _DESCRIPTION_MAX_LENGTH:
        raise invalid_definition_error(
            f"Description must not exceed {_DESCRIPTION_MAX_LENGTH} characters"
        )
    return value


def _validate_label(label: str, *, kind: str) -> str:
    value = _normalize_label(label, kind=kind)
    if len(value) > _LABEL_MAX_LENGTH:
        raise invalid_definition_error(
            f"{kind} label must not exceed {_LABEL_MAX_LENGTH} characters"
        )
    return value


def _normalize_options(
    field: DataFieldInput,
    *,
    label: str,
) -> tuple[EnumOptionSpec, ...]:
    """校验枚举选项：只有 ENUM 可用、至少一个、至多 1000、标签唯一。"""
    if field.field_type != DataFieldType.ENUM:
        if field.options:
            raise invalid_definition_error(
                f"Field {label} is not an enum and cannot have options"
            )
        return ()

    if not field.options:
        raise invalid_definition_error(
            f"Enum field {label} requires at least one option"
        )
    if len(field.options) > _MAX_ENUM_OPTIONS:
        raise invalid_definition_error(
            f"Enum field {label} must not exceed {_MAX_ENUM_OPTIONS} options"
        )

    normalized: list[EnumOptionSpec] = []
    seen: set[str] = set()
    for option in field.options:
        option_label = _normalize_label(option.label, kind="Enum option")
        if len(option_label) > _OPTION_LABEL_MAX_LENGTH:
            raise invalid_definition_error(
                "Enum option label must not exceed "
                f"{_OPTION_LABEL_MAX_LENGTH} characters"
            )
        lookup_key = option_label.casefold()
        if lookup_key in seen:
            raise invalid_definition_error(
                f"Enum option {option_label} is duplicated in field {label}"
            )
        seen.add(lookup_key)
        normalized.append(
            EnumOptionSpec(label=option_label, sort_order=option.sort_order)
        )
    return tuple(normalized)


def _validate_field(field: DataFieldInput) -> FieldSpec:
    """把一个字段输入转换成待落库的规格并校验其类型规则。"""
    if field.system_key == DataFieldRole.CUSTOM:
        label = _validate_label(field.label, kind="Field")
        if field.field_type not in _CUSTOM_FIELD_TYPES:
            raise invalid_definition_error(
                f"Custom field {label} has an unsupported field type "
                f"{field.field_type.value}"
            )
        return FieldSpec(
            label=label,
            field_type=field.field_type,
            system_key=DataFieldRole.CUSTOM,
            required=field.required,
            sort_order=field.sort_order,
            options=_normalize_options(field, label=label),
        )

    label = _validate_label(field.label, kind="Field")
    expected_type = _FIXED_ROLE_TYPES[field.system_key]
    if field.field_type != expected_type:
        raise invalid_definition_error(
            f"System field {field.system_key.value} must be {expected_type.value}"
        )
    if field.options:
        raise invalid_definition_error(
            f"System field {field.system_key.value} cannot have enum options"
        )
    # 固定字段恒为必填：输入里的 required 一律忽略，不交给调用方决定。
    return FieldSpec(
        label=label,
        field_type=expected_type,
        system_key=field.system_key,
        required=True,
        sort_order=field.sort_order,
    )


def _validate_fields(fields: Sequence[DataFieldInput]) -> list[FieldSpec]:
    """校验完整字段集合：五个固定角色各一个、标签唯一、自定义字段上限。"""
    specs = [_validate_field(field) for field in fields]

    seen_roles: dict[DataFieldRole, int] = {}
    seen_labels: dict[str, str] = {}
    custom_count = 0
    for spec in specs:
        if spec.system_key == DataFieldRole.CUSTOM:
            custom_count += 1
        else:
            if spec.system_key in seen_roles:
                raise invalid_definition_error(
                    f"System field {spec.system_key.value} is duplicated"
                )
            seen_roles[spec.system_key] = 1

        # 标签唯一性与数据库排序规则保持一致：忽略大小写与首尾空白。
        lookup_key = spec.label.casefold()
        if lookup_key in seen_labels:
            raise invalid_definition_error(f"Field label {spec.label} is duplicated")
        seen_labels[lookup_key] = spec.label

    missing = [role.value for role in _FIXED_ROLE_TYPES if role not in seen_roles]
    if missing:
        raise invalid_definition_error(
            "Missing required system fields: " + ", ".join(sorted(missing))
        )
    if custom_count > _MAX_CUSTOM_FIELDS:
        raise invalid_definition_error(
            f"Custom field count must not exceed {_MAX_CUSTOM_FIELDS}"
        )
    # 保持提交顺序：sort_order 相同（或未提供，默认 0）时按输入顺序展示，
    # 这样省略 sort_order 的请求仍然得到调用方期望的列顺序。
    return specs


def build_field_specs(payload: DataTableCreate) -> list[FieldSpec]:
    """把请求模型规范化并校验成待落库的字段规格列表。"""
    return _validate_fields(payload.fields)


async def _require_writable_table(
    session: AsyncSession,
    table_id: int,
) -> DataTableDefinition:
    """加锁读取表并校验它仍可修改。

    必须先取表行锁再检查导入，否则并发导入可能在检查之后入队，
    导致任务按过期定义执行（见设计 6.4）。检查顺序固定为
    “表行锁 → 未完成导入 → 成功数据”，与仓储层守卫保持一致：
    有排队任务时报 9008，有成功数据时报 9003。
    """
    table = await data_table_repository.get_table_for_update(session, table_id)
    if table is None:
        raise _not_found_error()
    if table.status != DataTableStatus.DRAFT:
        raise _locked_error()
    if await data_table_repository.has_unfinished_import(session, table_id):
        raise _import_in_progress_error()
    if await data_table_repository.has_successful_data(session, table_id):
        raise _data_exists_error()
    return table


async def _load_written_definition(
    session: AsyncSession,
    table_id: int,
) -> ResolvedDataDefinition:
    """Resolve a just-flushed write inside its still-open transaction."""
    definition = await data_table_repository.load_resolved_definition(
        session,
        table_id,
    )
    if definition is None:
        raise RuntimeError(f"Written data table {table_id} could not be resolved")
    return definition


@audited(
    action="data_table:create",
    resource_type="custom_data_table",
    resource_id_attr="id",
)
async def create_table(
    session: AsyncSession,
    payload: DataTableCreate,
    *,
    context: AuthContext,
) -> ResolvedDataDefinition:
    """创建完整 DRAFT 定义，提交并返回事务内解析结果。"""
    name = _normalize_name(payload.name)
    description = _normalize_description(payload.description)
    fields = build_field_specs(payload)
    table = await data_table_repository.create_table(
        session,
        name=name,
        description=description,
        fields=fields,
        created_by=context.user_id,
    )
    definition = await _load_written_definition(session, table.id)
    await session.commit()
    return definition


async def get_table(
    session: AsyncSession,
    table_id: int,
    *,
    context: AuthContext,
) -> ResolvedDataDefinition:
    """读取单张表的解析后定义。"""
    definition = await data_table_repository.load_resolved_definition(
        session,
        table_id,
    )
    if definition is None:
        raise _not_found_error()
    return definition


async def list_tables(
    session: AsyncSession,
    *,
    context: AuthContext,
) -> list[ResolvedDataDefinition]:
    """列出全部表定义；表定义对所有有查看权限的人可见。"""
    return await data_table_repository.load_resolved_definitions(session)


@audited(
    action="data_table:update",
    resource_type="custom_data_table",
    resource_id_param="table_id",
)
async def replace_table(
    session: AsyncSession,
    table_id: int,
    payload: DataTableCreate,
    *,
    context: AuthContext,
) -> ResolvedDataDefinition:
    """整体替换 DRAFT 表定义，提交并返回事务内解析结果。"""
    table = await _require_writable_table(session, table_id)
    name = _normalize_name(payload.name)
    description = _normalize_description(payload.description)
    fields = build_field_specs(payload)
    replaced = await data_table_repository.replace_table(
        session,
        table,
        name=name,
        description=description,
        fields=fields,
        updated_by=context.user_id,
    )
    definition = await _load_written_definition(session, replaced.id)
    await session.commit()
    return definition


@audited(
    action="data_table:delete",
    resource_type="custom_data_table",
    resource_id_param="table_id",
)
async def delete_table(
    session: AsyncSession,
    table_id: int,
    *,
    context: AuthContext,
) -> None:
    """物理删除没有成功数据的 DRAFT 表并提交。"""
    table = await _require_writable_table(session, table_id)
    await data_table_repository.delete_table(session, table)
    await session.commit()


__all__: tuple[str, ...] = (
    "build_field_specs",
    "create_table",
    "delete_table",
    "get_table",
    "invalid_definition_error",
    "list_tables",
    "replace_table",
)
