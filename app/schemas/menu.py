"""菜单请求与响应模型。"""

from pydantic import BaseModel, ConfigDict, Field

from app.models.menu import MenuStatus, MenuType


class MenuCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    type: MenuType
    name: str = Field(min_length=1, max_length=100)
    path: str | None = Field(default=None, max_length=255)
    component: str | None = Field(default=None, max_length=255)
    icon: str | None = Field(default=None, max_length=100)
    permission_code: str | None = Field(default=None, max_length=100)
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
    permission_code: str | None = Field(default=None, max_length=100)
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
    children: list[MenuNode] = Field(default_factory=list)


MenuNode.model_rebuild()


__all__: tuple[str, ...] = ("MenuCreate", "MenuNode", "MenuUpdate")
