"""系统监控采样领域模型。"""

from datetime import datetime

from sqlalchemy import Boolean, Float
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemMetric(Base):
    """记录系统与应用探活指标快照。"""

    __tablename__ = "system_metric"

    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False)
    memory_percent: Mapped[float] = mapped_column(Float, nullable=False)
    memory_used_mb: Mapped[float] = mapped_column(Float, nullable=False)
    memory_total_mb: Mapped[float] = mapped_column(Float, nullable=False)
    disk_percent: Mapped[float] = mapped_column(Float, nullable=False)
    disk_used_mb: Mapped[float] = mapped_column(Float, nullable=False)
    disk_total_mb: Mapped[float] = mapped_column(Float, nullable=False)
    process_memory_mb: Mapped[float] = mapped_column(Float, nullable=False)
    db_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    redis_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        nullable=False,
        index=True,
    )


__all__: tuple[str, ...] = ("SystemMetric",)
