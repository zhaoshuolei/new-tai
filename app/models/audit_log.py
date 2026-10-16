"""操作日志领域模型。"""

from enum import StrEnum

from sqlalchemy import BigInteger, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditResult(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"


class AuditLog(Base):
    """记录业务对象写操作及安全拒绝。"""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_user_id_created_at", "user_id", "created_at"),
        Index(
            "ix_audit_log_resource_type_resource_id",
            "resource_type",
            "resource_id",
        ),
        Index("ix_audit_log_action_created_at", "action", "created_at"),
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )
    actor_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )
    result: Mapped[AuditResult] = mapped_column(
        Enum(
            AuditResult,
            name="audit_result",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


__all__: tuple[str, ...] = ("AuditLog", "AuditResult")
