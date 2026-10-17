"""客户对接人与职能请求、响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ContactFunctionCreate(BaseModel):
    """新增职能；名称由服务层校验并返回 7003。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        json_schema_extra={"minLength": 1, "maxLength": 64},
    )


class ContactFunctionUpdate(BaseModel):
    """修改职能名称；名称由服务层校验并返回 7003。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        json_schema_extra={"minLength": 1, "maxLength": 64},
    )


class ContactFunctionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    customer_id: int | None
    scope: Literal["SHARED", "CUSTOM"]
    created_at: datetime
    updated_at: datetime


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    function_id: int
    function_name: str
    function_scope: Literal["SHARED", "CUSTOM"]
    name: str
    position: str | None
    phone: str | None
    email: str | None
    home_address: str | None
    remark: str | None
    created_at: datetime
    updated_at: datetime


class ContactCreate(BaseModel):
    """新增客户对接人；业务字段由服务层校验并返回 7003。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    function_id: int | None = None
    position: str | None = None
    phone: str | None = None
    email: str | None = None
    home_address: str | None = None
    remark: str | None = None


class ContactUpdate(BaseModel):
    """部分更新客户对接人；未提供的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    function_id: int | None = None
    position: str | None = None
    phone: str | None = None
    email: str | None = None
    home_address: str | None = None
    remark: str | None = None


__all__: tuple[str, ...] = (
    "ContactCreate",
    "ContactFunctionCreate",
    "ContactFunctionRead",
    "ContactFunctionUpdate",
    "ContactRead",
    "ContactUpdate",
)
