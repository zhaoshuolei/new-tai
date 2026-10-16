"""异常日志领域模型。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, Enum, Integer, String, false
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExceptionLevel(StrEnum):
    ERROR = "error"
    CRITICAL = "critical"


class ExceptionLog(Base):
    """记录服务端异常及处理状态。"""

    __tablename__ = "exception_log"

    exception_type: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    message: Mapped[str] = mapped_column(String(1024), nullable=False)
    fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    level: Mapped[ExceptionLevel] = mapped_column(
        Enum(
            ExceptionLevel,
            name="exception_level",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        index=True,
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        index=True,
    )
    acknowledged: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
        index=True,
    )
    acknowledged_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
    )


__all__: tuple[str, ...] = ("ExceptionLevel", "ExceptionLog")
