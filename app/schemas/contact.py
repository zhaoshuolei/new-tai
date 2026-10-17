"""客户对接人请求模型。"""

from pydantic import BaseModel, ConfigDict


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


__all__: tuple[str, ...] = ("ContactCreate", "ContactUpdate")
