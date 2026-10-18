"""跟进记录领域模型：跟进方式、跟进记录与附件。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.contact import ContactStatus


class ContactFollowUpMethod(Base):
    """跟进方式，逻辑删除后允许重新创建同名方式。"""

    __tablename__ = "contact_follow_up_method"
    __table_args__ = (
        UniqueConstraint(
            "active_name",
            name="uq_contact_follow_up_method_active_name",
        ),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
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


class ContactFollowUp(Base):
    """客户联系人的跟进记录。"""

    __tablename__ = "contact_follow_up"
    __table_args__ = (
        Index(
            "ix_contact_follow_up_contact_status_time",
            "contact_id",
            "status",
            "follow_up_at",
        ),
        Index(
            "ix_contact_follow_up_status_next",
            "status",
            "next_follow_up_at",
        ),
    )

    contact_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customer_contact.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    method_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("contact_follow_up_method.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    follow_up_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    follow_up_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    next_follow_up_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
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
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ContactFollowUpAttachment(Base):
    """跟进记录附件，按存储路径标识文件。"""

    __tablename__ = "contact_follow_up_attachment"

    follow_up_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("contact_follow_up.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    storage_path: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        unique=True,
    )
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__: tuple[str, ...] = (
    "ContactFollowUp",
    "ContactFollowUpAttachment",
    "ContactFollowUpMethod",
)
