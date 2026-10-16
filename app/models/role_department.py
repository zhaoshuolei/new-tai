"""角色自定义数据范围关联表。"""

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Table

from app.db.base import Base

RoleDepartment = Table(
    "role_department",
    Base.metadata,
    Column(
        "role_id",
        BigInteger,
        ForeignKey("role.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "department_id",
        BigInteger,
        ForeignKey("department.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Index("ix_role_department_department_id", "department_id"),
)


__all__: tuple[str, ...] = ("RoleDepartment",)
