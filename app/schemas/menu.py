"""菜单请求与响应模型。"""

import re
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.menu import MenuStatus, MenuType

PERMISSION_CODE_ACTIONS = frozenset(
    {"create", "read", "update", "delete", "assign", "import"}
)
_PERMISSION_CODE_PATTERN = re.compile(r"^([a-z][a-z_]*):([a-z]+)$")


def _validate_permission_code(value: str) -> str:
    match = _PERMISSION_CODE_PATTERN.fullmatch(value)
    if match is None or match.group(2) not in PERMISSION_CODE_ACTIONS:
        raise ValueError(
            "permission_code must use resource:action with a supported action"
        )
    return value


PermissionCode = Annotated[
    str,
    Field(max_length=100),
    AfterValidator(_validate_permission_code),
]


class MenuCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    type: MenuType
    name: str = Field(min_length=1, max_length=100)
    path: str | None = Field(default=None, max_length=255)
    component: str | None = Field(default=None, max_length=255)
    icon: str | None = Field(default=None, max_length=100)
    permission_code: PermissionCode | None = None
    sort_order: int = 0
    visible: bool = True


class MenuUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    type: MenuType | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    path: str | None = Field(default=None, max_length=255)
    component: str | None = Field(default=None, max_length=255)
    icon: str | None = Field(default=None, max_length=100)
    permission_code: PermissionCode | None = None
    sort_order: int | None = None
    visible: bool | None = None
    status: MenuStatus | None = None


class MenuNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    parent_id: int | None
    type: MenuType
    name: str
    path: str | None
    component: str | None
    icon: str | None
    permission_code: str | None
    sort_order: int
    visible: bool
    status: MenuStatus
    deleted_at: datetime | None = None
    deleted_by: int | None = None
    children: list[MenuNode] = Field(default_factory=list)


MenuNode.model_rebuild()


__all__: tuple[str, ...] = (
    "PERMISSION_CODE_ACTIONS",
    "MenuCreate",
    "MenuNode",
    "MenuUpdate",
    "PermissionCode",
)
