"""客户对接人请求模型。"""

from pydantic import BaseModel, ConfigDict, Field


class ContactCreate(BaseModel):
    """新增客户对接人。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    function_id: int
    position: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    home_address: str | None = Field(default=None, max_length=512)
    remark: str | None = None


class ContactUpdate(BaseModel):
    """部分更新客户对接人；未提供的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    function_id: int | None = None
    position: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    home_address: str | None = Field(default=None, max_length=512)
    remark: str | None = None


__all__: tuple[str, ...] = ("ContactCreate", "ContactUpdate")
