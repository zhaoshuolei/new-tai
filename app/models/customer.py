"""客户领域模型。"""

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Enum,
    ForeignKey,
    String,
    Text,
    false,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CustomerSource(StrEnum):
    TIANYANCHA = "TIANYANCHA"
    MANUAL = "MANUAL"


class Customer(Base):
    """客户档案，统一社会信用代码非空时全局唯一。"""

    __tablename__ = "customer"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credit_code: Mapped[str | None] = mapped_column(
        String(18),
        nullable=True,
        unique=True,
        index=True,
    )
    legal_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registered_capital: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    established_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    business_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
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
