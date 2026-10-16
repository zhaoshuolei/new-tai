"""客户请求与响应模型。"""

from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.customer import CustomerSource


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class CustomerCreate(BaseModel):
    """手工创建客户；信用代码可为空，格式由服务层校验。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    short_name: str | None = Field(default=None, max_length=255)
    credit_code: str | None = None
    legal_person: str | None = Field(default=None, max_length=255)
    registered_capital: str | None = Field(default=None, max_length=255)
    established_date: date | None = None
    business_scope: str | None = None
    reg_status: str | None = Field(default=None, max_length=64)
    remark: str | None = None


class CustomerUpdate(BaseModel):
    """部分更新客户；未提供的字段保持不变。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    short_name: str | None = Field(default=None, max_length=255)
    credit_code: str | None = None
    legal_person: str | None = Field(default=None, max_length=255)
    registered_capital: str | None = Field(default=None, max_length=255)
    established_date: date | None = None
    business_scope: str | None = None
    reg_status: str | None = Field(default=None, max_length=64)
    remark: str | None = None


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    short_name: str | None
    credit_code: str | None
    legal_person: str | None
    registered_capital: str | None
    established_date: date | None
    business_scope: str | None
    business_scope_truncated: bool
    reg_status: str | None
    source: CustomerSource
    tyc_imported_at: datetime | None
    remark: str | None
    owner_user_id: int
    owner_department_id: int | None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "tyc_imported_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


__all__: tuple[str, ...] = (
    "CustomerCreate",
    "CustomerRead",
    "CustomerUpdate",
)
