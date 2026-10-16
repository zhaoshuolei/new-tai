from datetime import datetime

from sqlalchemy import BigInteger, func
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        server_default=func.utc_timestamp(6),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        server_default=func.utc_timestamp(6),
        onupdate=func.utc_timestamp(6),
    )


__all__: tuple[str, ...] = ("Base",)
