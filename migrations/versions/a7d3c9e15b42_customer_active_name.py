"""customer active name

Revision ID: a7d3c9e15b42
Revises: c375c8de4899
Create Date: 2026-10-18 21:20:00.000000

为未删除客户增加去首尾空白后的唯一名称（设计 7.1）。迁移不合并、不改名；
存量活跃重名会阻断升级，只报告重复组数量与修复命令。
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "a7d3c9e15b42"
down_revision: str | Sequence[str] | None = "c375c8de4899"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SAFE_TABLE_NAME = re.compile(r"[a-z0-9_]+")


def _assert_no_duplicate_active_names(
    connection: Connection,
    *,
    table_name: str = "customer",
) -> None:
    """在 DDL 之前阻断存量活跃重名。

    分组沿用名称列的 ``utf8mb4_0900_ai_ci`` 排序规则，因此大小写、重音与首尾
    空白差异都算重名。错误消息只包含重复组数量与修复命令，不包含客户名称。
    """
    if _SAFE_TABLE_NAME.fullmatch(table_name) is None:
        raise ValueError("Unexpected table name for customer name check")

    quoted_table = connection.dialect.identifier_preparer.quote(table_name)
    group_count = connection.scalar(
        sa.text(
            f"""
            SELECT COUNT(*) FROM (
                SELECT TRIM(name) AS normalized_name
                FROM {quoted_table}
                WHERE status = 'ACTIVE'
                GROUP BY normalized_name
                HAVING COUNT(*) > 1
            ) AS duplicate_names
            """
        )
    )
    if group_count:
        raise RuntimeError(
            f"Cannot enforce unique active customer names: {int(group_count)} "
            "duplicate group(s) of active customer names exist. "
            "Run `python -m scripts.check_duplicate_customer_names` to list them, "
            "merge or delete the duplicates, then retry `alembic upgrade head`."
        )


def upgrade() -> None:
    _assert_no_duplicate_active_names(op.get_bind())
    op.add_column(
        "customer",
        sa.Column(
            "active_name",
            sa.String(length=255),
            sa.Computed(
                "CASE WHEN status = 'ACTIVE' THEN TRIM(name) END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_customer_active_name"),
        "customer",
        ["active_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_customer_active_name"), table_name="customer")
    op.drop_column("customer", "active_name")
