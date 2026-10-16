"""第三方集成配置领域模型。"""

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IntegrationConfig(Base):
    """保存第三方集成的加密密钥配置，每个供应商仅一条记录。"""

    __tablename__ = "integration_config"

    provider: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    # 审计字段，仅记录最后修改人，不建立外键，与日志表语义一致。
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


__all__: tuple[str, ...] = ("IntegrationConfig",)
