"""菜单领域模型。"""

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MenuType(StrEnum):
    DIR = "DIR"
    MENU = "MENU"
    BUTTON = "BUTTON"


class MenuStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Menu(Base):
    __tablename__ = "menu"

    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("menu.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    type: Mapped[MenuType] = mapped_column(
        Enum(
            MenuType,
            name="menu_type",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    component: Mapped[str | None] = mapped_column(String(255), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(100), nullable=True)
    permission_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        unique=True,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    visible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    status: Mapped[MenuStatus] = mapped_column(
        Enum(
            MenuStatus,
            name="menu_status",
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
        default=MenuStatus.ACTIVE,
        server_default=MenuStatus.ACTIVE.value,
    )


__all__: tuple[str, ...] = ("Menu", "MenuStatus", "MenuType")
