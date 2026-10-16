"""登录日志领域模型。"""

from enum import StrEnum

from sqlalchemy import BigInteger, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LoginType(StrEnum):
    LOCAL = "local"
    FEISHU = "feishu"


class LoginResultEnum(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class LoginLog(Base):
    """记录本地与飞书登录尝试。"""

    __tablename__ = "login_log"
    __table_args__ = (Index("ix_login_log_created_at", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )
    username: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )
    login_type: Mapped[LoginType] = mapped_column(
        Enum(
            LoginType,
            name="login_type",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        index=True,
    )
    result: Mapped[LoginResultEnum] = mapped_column(
        Enum(
            LoginResultEnum,
            name="login_result",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


__all__: tuple[str, ...] = ("LoginLog", "LoginResultEnum", "LoginType")
