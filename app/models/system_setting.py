"""系统基础设置领域模型。"""

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemSetting(Base):
    """保存全局站点身份信息，表中仅允许一个 ``global`` 配置。"""

    __tablename__ = "system_setting"

    setting_key: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    site_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    favicon_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    copyright: Mapped[str | None] = mapped_column(String(200), nullable=True)
    icp_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


__all__: tuple[str, ...] = ("SystemSetting",)
