"""跟进记录请求模型；业务字段由服务层校验并返回 7003。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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


__all__: tuple[str, ...] = (
    "FollowUpCreate",
    "FollowUpUpdate",
)
