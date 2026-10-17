"""角色请求与响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.role import DataScope, RoleStatus


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    data_scope: DataScope = DataScope.SELF
    data_scope_include_children: bool = True


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    status: RoleStatus | None = None


class RoleMenuAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    menu_ids: list[int] = Field(default_factory=list)


class RoleAssignmentsRead(BaseModel):
    menu_ids: list[int]
    department_ids: list[int]


class RoleDataScopeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_scope: DataScope
    data_scope_include_children: bool = True
    department_ids: list[int] = Field(default_factory=list)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    data_scope: DataScope
    data_scope_include_children: bool
    is_system: bool
    status: RoleStatus
    deleted_at: datetime | None = None
    deleted_by: int | None = None


__all__: tuple[str, ...] = (
    "RoleAssignmentsRead",
    "RoleCreate",
    "RoleDataScopeUpdate",
    "RoleMenuAssign",
    "RoleRead",
    "RoleUpdate",
)
