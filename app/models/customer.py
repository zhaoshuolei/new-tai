"""客户领域模型。"""

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    Enum,
    ForeignKey,
    String,
    Text,
    false,
)
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.contact import ContactStatus


class CustomerSource(StrEnum):
    TIANYANCHA = "TIANYANCHA"
    MANUAL = "MANUAL"


class Customer(Base):
    """客户档案。未删除客户的信用代码与名称全局唯一。

    见 active_credit_code 与 active_name 两个生成列。
    """

    __tablename__ = "customer"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active_name: Mapped[str | None] = mapped_column(
        String(255, collation="utf8mb4_0900_ai_ci"),
        Computed(
            "CASE WHEN status = 'ACTIVE' THEN TRIM(name) END",
            persisted=True,
        ),
        index=True,
        unique=True,
    )
    short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credit_code: Mapped[str | None] = mapped_column(
        String(18),
        nullable=True,
    )
    active_credit_code: Mapped[str | None] = mapped_column(
        String(18),
        Computed(
            "CASE WHEN status = 'ACTIVE' THEN credit_code END",
            persisted=True,
        ),
        index=True,
        unique=True,
    )
    status: Mapped[ContactStatus] = mapped_column(
        Enum(
            ContactStatus,
            name="contact_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=ContactStatus.ACTIVE,
        server_default="ACTIVE",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legal_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registered_capital: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    established_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    business_scope: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    business_scope_truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    reg_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[CustomerSource] = mapped_column(
        Enum(
            CustomerSource,
            name="customer_source",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    tyc_imported_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_department_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("department.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


__all__: tuple[str, ...] = ("Customer", "CustomerSource")
