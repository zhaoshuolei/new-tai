"""操作与登录日志响应模型。"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.audit_log import AuditResult
from app.models.login_log import LoginResultEnum, LoginType


class _CreatedAtRead(BaseModel):
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return utc_value.astimezone(UTC).isoformat()


class AuditLogRead(_CreatedAtRead):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    actor_username: str | None
    actor_nickname: str | None
    action: str
    resource_type: str
    resource_id: int | None
    result: AuditResult
    reason: str | None
    method: str | None
    path: str | None
    ip: str | None
    request_id: str


class LoginLogRead(_CreatedAtRead):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    username: str | None
    login_type: LoginType
    result: LoginResultEnum
    reason: str | None
    ip: str | None
    user_agent: str | None
    device_id: str | None
    request_id: str


__all__: tuple[str, ...] = ("AuditLogRead", "LoginLogRead")
