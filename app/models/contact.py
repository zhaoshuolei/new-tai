"""联系人领域模型：联系人职能（函数）与客户联系人。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Computed,
    Enum,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContactStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class ContactFunctionNameLock(Base):
    """用于串行化职能名称校验与写入的单例行锁。"""

    __tablename__ = "contact_function_name_lock"

    id: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        autoincrement=False,
    )


class ContactFunction(Base):
    """联系人职能。customer_id 为空表示共用职能（scope_key=0）。"""

    __tablename__ = "contact_function"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "active_name",
            name="uq_contact_function_scope_name",
        ),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("customer.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scope_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    active_name: Mapped[str | None] = mapped_column(
        String(64),
        Computed("CASE WHEN status = 'ACTIVE' THEN name END", persisted=True),
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class CustomerContact(Base):
    """客户联系人，归属于客户并引用一个联系人职能。"""

    __tablename__ = "customer_contact"

    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customer.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    function_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("contact_function.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    home_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
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


__all__: tuple[str, ...] = (
    "ContactFunction",
    "ContactFunctionNameLock",
    "ContactStatus",
    "CustomerContact",
)
