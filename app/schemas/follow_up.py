"""跟进记录请求与响应模型；业务字段由服务层校验并返回 7003。"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class FollowUpMethodCreate(BaseModel):
    """新增跟进方式；名称由服务层校验并返回 7003。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        json_schema_extra={"minLength": 1, "maxLength": 64},
    )


class FollowUpMethodUpdate(BaseModel):
    """修改跟进方式名称；名称由服务层校验并返回 7003。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        json_schema_extra={"minLength": 1, "maxLength": 64},
    )


class FollowUpCreate(BaseModel):
    """新增跟进记录。"""

    model_config = ConfigDict(extra="forbid")

    follow_up_user_id: int | None = None
    follow_up_at: datetime | None = None
    method_id: int | None = None
    content: str | None = None
    next_follow_up_at: datetime | None = None


class FollowUpUpdate(BaseModel):
    """部分更新跟进记录；未提供的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    follow_up_user_id: int | None = None
    follow_up_at: datetime | None = None
    method_id: int | None = None
    content: str | None = None
    next_follow_up_at: datetime | None = None


class FollowUpMethodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class FollowUpAttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_name: str | None
    content_type: str
    size_bytes: int
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class FollowUpRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    contact_id: int
    contact_name: str
    method_id: int
    method_name: str
    follow_up_user_id: int
    follow_up_user_name: str
    created_by: int
    created_by_name: str
    follow_up_at: datetime
    content: str
    next_follow_up_at: datetime | None
    attachments: list[FollowUpAttachmentRead]
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "follow_up_at",
        "next_follow_up_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class DueFollowUpRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    follow_up_id: int
    due_state: Literal["today", "overdue"]
    next_follow_up_at: datetime
    follow_up_at: datetime
    customer_id: int
    customer_name: str
    contact_id: int
    contact_name: str
    function_name: str
    follow_up_user_id: int
    follow_up_user_name: str

    @field_serializer(
        "next_follow_up_at",
        "follow_up_at",
        when_used="json",
    )
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class DueCustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: int
    customer_name: str
    due_count: int
    overdue_count: int
    earliest_next_follow_up_at: datetime

    @field_serializer("earliest_next_follow_up_at", when_used="json")
    def serialize_earliest_next_follow_up_at(
        self,
        value: datetime | None,
    ) -> str | None:
        return _serialize_utc(value)


class DueSummaryRead(BaseModel):
    today: int
    overdue: int


__all__: tuple[str, ...] = (
    "DueCustomerRead",
    "DueFollowUpRead",
    "DueSummaryRead",
    "FollowUpAttachmentRead",
    "FollowUpCreate",
    "FollowUpMethodCreate",
    "FollowUpMethodRead",
    "FollowUpMethodUpdate",
    "FollowUpRead",
    "FollowUpUpdate",
)
