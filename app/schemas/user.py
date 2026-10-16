"""用户请求与响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import UserStatus


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)
    nickname: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    avatar: str | None = Field(default=None, max_length=512)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    avatar: str | None = Field(default=None, max_length=512)


class UserRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: int | None


class UserDepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: int | None


class UserStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UserStatus


class UserPasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=1, max_length=128)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    feishu_open_id: str | None
    username: str | None
    nickname: str | None
    email: str | None
    avatar: str | None
    department_id: int | None
    role_id: int | None
    status: UserStatus
    token_version: int
    last_login_at: datetime | None


__all__: tuple[str, ...] = (
    "UserCreate",
    "UserDepartmentUpdate",
    "UserPasswordReset",
    "UserRead",
    "UserRoleUpdate",
    "UserStatusUpdate",
    "UserUpdate",
)
