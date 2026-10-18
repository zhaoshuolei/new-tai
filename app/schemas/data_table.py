"""可配置数据表的请求与响应模型。

字段长度、空值与类型组合等业务规则由服务层校验并返回 9004，
这里只声明结构，避免把同一套规则实现两次（见设计 9.1）。
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.data_management import (
    DataFieldRole,
    DataFieldType,
    DataTableStatus,
)

if TYPE_CHECKING:
    from app.repositories.data_table import ResolvedDataDefinition


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class DataEnumOptionInput(BaseModel):
    """静态枚举选项输入；标签长度与唯一性由服务层校验。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    sort_order: int = 0


class DataFieldInput(BaseModel):
    """一个字段定义输入，固定角色与自定义字段共用同一结构。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    field_type: DataFieldType
    system_key: DataFieldRole
    required: bool = False
    sort_order: int = 0
    options: list[DataEnumOptionInput] = Field(default_factory=list)


class DataTableCreate(BaseModel):
    """创建表时一次提交表元数据与完整字段定义。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    fields: list[DataFieldInput]


class DataTableReplace(DataTableCreate):
    """DRAFT 阶段整体替换表定义；语义与创建一致。"""


class DataEnumOptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    sort_order: int


class DataFieldRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    field_type: DataFieldType
    system_key: DataFieldRole
    required: bool
    sort_order: int
    options: list[DataEnumOptionRead] = Field(default_factory=list)


class DataTableDefinitionRead(BaseModel):
    """表定义响应；字段按展示顺序排列并内嵌枚举选项。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    status: DataTableStatus
    locked_at: datetime | None
    created_by: int
    updated_by: int | None
    created_at: datetime
    updated_at: datetime
    fields: list[DataFieldRead]

    @classmethod
    def from_resolved(cls, definition: ResolvedDataDefinition) -> Self:
        table = definition.table
        return cls(
            id=table.id,
            name=table.name,
            description=table.description,
            status=table.status,
            locked_at=table.locked_at,
            created_by=table.created_by,
            updated_by=table.updated_by,
            created_at=table.created_at,
            updated_at=table.updated_at,
            fields=[
                DataFieldRead(
                    id=field.id,
                    label=field.label,
                    field_type=field.field_type,
                    system_key=field.system_key,
                    required=field.required,
                    sort_order=field.sort_order,
                    options=[
                        DataEnumOptionRead.model_validate(option)
                        for option in definition.options_for(field.id)
                    ],
                )
                for field in definition.fields
            ],
        )

    @field_serializer(
        "locked_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


__all__: tuple[str, ...] = (
    "DataEnumOptionInput",
    "DataEnumOptionRead",
    "DataFieldInput",
    "DataFieldRead",
    "DataTableCreate",
    "DataTableDefinitionRead",
    "DataTableReplace",
)
