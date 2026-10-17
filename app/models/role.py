"""角色领域模型。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    false,
    true,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.role_department import RoleDepartment


class DataScope(StrEnum):
    ALL = "ALL"
    DEPT_AND_CHILD = "DEPT_AND_CHILD"
    DEPT = "DEPT"
    SELF = "SELF"
    CUSTOM = "CUSTOM"


class RoleStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Role(Base):
    __tablename__ = "role"

    code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    active_code: Mapped[str | None] = mapped_column(
        String(100),
        Computed(
            "CASE WHEN deleted_at IS NULL THEN code END",
            persisted=True,
        ),
        unique=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    data_scope: Mapped[DataScope] = mapped_column(
        Enum(
            DataScope,
            name="role_data_scope",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=DataScope.SELF,
        server_default=DataScope.SELF.value,
    )
    data_scope_include_children: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    status: Mapped[RoleStatus] = mapped_column(
        Enum(
            RoleStatus,
            name="role_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=RoleStatus.ACTIVE,
        server_default=RoleStatus.ACTIVE.value,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6),
        nullable=True,
        index=True,
    )
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


RoleMenu = Table(
    "role_menu",
    Base.metadata,
    Column(
        "role_id",
        BigInteger,
        ForeignKey("role.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "menu_id",
        BigInteger,
        ForeignKey("menu.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_role_menu_menu_id", "menu_id"),
)


__all__: tuple[str, ...] = (
    "DataScope",
    "Role",
    "RoleDepartment",
    "RoleMenu",
    "RoleStatus",
)
