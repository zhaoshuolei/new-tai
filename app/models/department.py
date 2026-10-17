"""部门领域模型。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    false,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DepartmentStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Department(Base):
    __tablename__ = "department"

    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("department.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    status: Mapped[DepartmentStatus] = mapped_column(
        Enum(
            DepartmentStatus,
            name="department_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=DepartmentStatus.ACTIVE,
        server_default=DepartmentStatus.ACTIVE.value,
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
        index=True,
    )
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


__all__: tuple[str, ...] = ("Department", "DepartmentStatus")
