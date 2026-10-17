"""部门请求与响应模型。"""

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> Self:
        for field_name in ("name", "sort_order", "status"):
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self


class DepartmentMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_parent_id: int | None


class DepartmentNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    parent_id: int | None
    name: str
    path: str
    sort_order: int
    status: DepartmentStatus
    is_system: bool
    deleted_at: datetime | None = None
    deleted_by: int | None = None
    children: list[DepartmentNode] = Field(default_factory=list)


DepartmentNode.model_rebuild()


__all__: tuple[str, ...] = (
    "DepartmentCreate",
    "DepartmentMove",
    "DepartmentNode",
    "DepartmentUpdate",
)
