"""用户自助档案领域模型。"""

from datetime import date
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PUBLIC_PROFILE_FIELD_KEYS: tuple[str, ...] = (
    "signature",
    "bio",
    "work_phone",
    "extension",
    "work_email",
    "office_location",
    "seat",
    "expertise_tags",
    "personal_links",
    "avatar",
)


class UserProfile(Base):
    """保存员工的自助档案资料。"""

    __tablename__ = "user_profile"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    signature: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extension: Mapped[str | None] = mapped_column(String(16), nullable=True)
    work_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    office_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    seat: Mapped[str | None] = mapped_column(String(50), nullable=True)
    expertise_tags: Mapped[list[Any]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    personal_links: Mapped[list[Any]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    hidden_public_fields: Mapped[list[Any]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    personal_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    emergency_contact_name: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    emergency_contact_phone: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )


__all__: tuple[str, ...] = ("PUBLIC_PROFILE_FIELD_KEYS", "UserProfile")
