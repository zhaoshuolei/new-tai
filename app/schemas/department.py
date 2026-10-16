"""部门请求与响应模型。"""

from pydantic import BaseModel, ConfigDict, Field

from app.models.department import DepartmentStatus


class DepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    parent_id: int | None = None
    sort_order: int = 0


class DepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    status: DepartmentStatus | None = None


class DepartmentNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    parent_id: int | None
    name: str
    path: str
    sort_order: int
    status: DepartmentStatus
    is_system: bool
    children: list[DepartmentNode] = Field(default_factory=list)


DepartmentNode.model_rebuild()


__all__: tuple[str, ...] = (
    "DepartmentCreate",
    "DepartmentNode",
    "DepartmentUpdate",
)
