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


class CustomerImportRequest(BaseModel):
    """天眼查回填请求；信用代码格式由服务层校验。"""

    model_config = ConfigDict(extra="forbid")

    credit_code: str = Field(min_length=1, max_length=64)
    short_name: str | None = Field(default=None, max_length=255)


class CustomerOwnerUpdate(BaseModel):
    """转移客户负责人请求。"""

    model_config = ConfigDict(extra="forbid")

    owner_user_id: int


class CustomerSearchItem(BaseModel):
    """天眼查候选企业；已存在标注随可见性变化。"""

    company_id: str
    name: str
    credit_code: str | None
    legal_person: str | None
    reg_status: str | None
    existing_customer_id: int | None = None
    existed_hidden: bool = False


class CustomerSearchResponse(BaseModel):
    items: list[CustomerSearchItem]
    total: int
    page: int
    page_size: int


class IntegrationTokenUpdate(BaseModel):
    """天眼查 token 更新请求，明文不进入 repr。"""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=256, repr=False)


class IntegrationStatus(BaseModel):
    """天眼查配置状态，不含任何 token 明文或密文。"""

    model_config = ConfigDict(from_attributes=True)

    configured: bool
    updated_at: datetime | None
    updated_by: int | None

    @field_serializer("updated_at", when_used="json")
    def serialize_updated_at(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


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
    "CustomerImportRequest",
    "CustomerOwnerUpdate",
    "CustomerRead",
    "CustomerSearchItem",
    "CustomerSearchResponse",
    "CustomerUpdate",
    "IntegrationStatus",
    "IntegrationTokenUpdate",
)
