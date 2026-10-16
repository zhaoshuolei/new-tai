"""认证接口的请求与响应模型。"""

from pydantic import BaseModel, ConfigDict, Field

from app.models.role import DataScope
from app.schemas.user import UserRead


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, max_length=512)


class TokenRead(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class LoginRead(TokenRead):
    profile: UserRead


class CurrentUserRead(UserRead):
    data_scope: DataScope | None
    custom_department_ids: list[int]
    include_children: bool
    permission_codes: list[str]
    is_super_admin: bool


__all__: tuple[str, ...] = (
    "CurrentUserRead",
    "LoginRead",
    "LoginRequest",
    "LogoutRequest",
    "RefreshRequest",
    "TokenRead",
)
