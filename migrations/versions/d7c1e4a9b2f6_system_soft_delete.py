"""system resources soft delete

Revision ID: d7c1e4a9b2f6
Revises: b5db85b8adc4
Create Date: 2026-10-17 16:00:00.000000
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "d7c1e4a9b2f6"
down_revision: str | Sequence[str] | None = "b5db85b8adc4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.add_column("user", sa.Column("deleted_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "user",
        sa.Column(
            "active_feishu_open_id",
            sa.String(length=128),
            sa.Computed(
                "CASE WHEN deleted_at IS NULL THEN feishu_open_id END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "active_username",
            sa.String(length=100),
            sa.Computed(
                "CASE WHEN deleted_at IS NULL THEN username END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.drop_index(op.f("ix_user_feishu_open_id"), table_name="user")
    op.drop_index(op.f("ix_user_username"), table_name="user")
    op.create_index(op.f("ix_user_deleted_at"), "user", ["deleted_at"], unique=False)
    op.create_index(
        op.f("ix_user_active_feishu_open_id"),
        "user",
        ["active_feishu_open_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_user_active_username"),
        "user",
        ["active_username"],
        unique=True,
    )

    op.add_column(
        "role",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.add_column("role", sa.Column("deleted_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "role",
        sa.Column(
            "active_code",
            sa.String(length=100),
            sa.Computed(
                "CASE WHEN deleted_at IS NULL THEN code END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.drop_index(op.f("ix_role_code"), table_name="role")
    op.create_index(op.f("ix_role_deleted_at"), "role", ["deleted_at"], unique=False)
    op.create_index(
        op.f("ix_role_active_code"),
        "role",
        ["active_code"],
        unique=True,
    )

    op.add_column(
        "department",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.add_column(
        "department",
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        op.f("ix_department_deleted_at"),
        "department",
        ["deleted_at"],
        unique=False,
    )

    op.add_column(
        "menu",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.add_column("menu", sa.Column("deleted_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "menu",
        sa.Column(
            "active_permission_code",
            sa.String(length=100),
            sa.Computed(
                "CASE WHEN deleted_at IS NULL THEN permission_code END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.drop_index(op.f("ix_menu_permission_code"), table_name="menu")
    op.create_index(op.f("ix_menu_deleted_at"), "menu", ["deleted_at"], unique=False)
    op.create_index(
        op.f("ix_menu_active_permission_code"),
        "menu",
        ["active_permission_code"],
        unique=True,
    )


def _null_deleted_duplicate_values(
    connection: sa.Connection,
    *,
    table_name: str,
    column_name: str,
) -> None:
    preparer = connection.dialect.identifier_preparer
    table = preparer.quote(table_name)
    column = preparer.quote(column_name)
    connection.execute(
        sa.text(
            f"""
            UPDATE {table} AS target
            JOIN (
                SELECT *
                FROM (
                    SELECT {column},
                           COALESCE(
                               MAX(CASE WHEN deleted_at IS NULL THEN id END),
                               MIN(id)
                           ) AS keep_id
                    FROM {table}
                    WHERE {column} IS NOT NULL
                    GROUP BY {column}
                    HAVING COUNT(*) > 1
                ) AS grouped
            ) AS duplicate
              ON duplicate.{column} = target.{column}
            SET target.{column} = NULL
            WHERE target.id <> duplicate.keep_id
            """
        )
    )


def _rename_deleted_duplicate_role_codes(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            UPDATE `role` AS target
            JOIN (
                SELECT code,
                       COALESCE(
                           MAX(CASE WHEN deleted_at IS NULL THEN id END),
                           MIN(id)
                       ) AS keep_id
                FROM `role`
                GROUP BY code
                HAVING COUNT(*) > 1
            ) AS duplicate ON duplicate.code = target.code
            SET target.code = CONCAT(
                LEFT(target.code, 68),
                '__deleted_',
                target.id
            )
            WHERE target.id <> duplicate.keep_id
            """
        )
    )


_SOFT_DELETE_TABLES = ("user", "role", "department", "menu")


def _ensure_no_soft_deleted_rows(
    connection: sa.Connection,
    *,
    table_names: Iterable[str] = _SOFT_DELETE_TABLES,
) -> None:
    preparer = connection.dialect.identifier_preparer
    deleted_rows: list[str] = []
    for table_name in table_names:
        table = preparer.quote(table_name)
        count = connection.scalar(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NOT NULL")
        )
        if count:
            deleted_rows.append(f"{table_name}={count}")
    if deleted_rows:
        raise RuntimeError(
            "Cannot downgrade system soft-delete migration while soft-deleted "
            f"rows exist ({', '.join(deleted_rows)}). Restore or purge them first."
        )


def downgrade() -> None:
    _ensure_no_soft_deleted_rows(op.get_bind())
    op.drop_index(op.f("ix_menu_active_permission_code"), table_name="menu")
    op.drop_index(op.f("ix_menu_deleted_at"), table_name="menu")
    _null_deleted_duplicate_values(
        op.get_bind(),
        table_name="menu",
        column_name="permission_code",
    )
    op.create_index(
        op.f("ix_menu_permission_code"),
        "menu",
        ["permission_code"],
        unique=True,
    )
    op.drop_column("menu", "active_permission_code")
    op.drop_column("menu", "deleted_by")
    op.drop_column("menu", "deleted_at")

    op.drop_index(op.f("ix_department_deleted_at"), table_name="department")
    op.drop_column("department", "deleted_by")
    op.drop_column("department", "deleted_at")

    op.drop_index(op.f("ix_role_active_code"), table_name="role")
    op.drop_index(op.f("ix_role_deleted_at"), table_name="role")
    _rename_deleted_duplicate_role_codes(op.get_bind())
    op.create_index(op.f("ix_role_code"), "role", ["code"], unique=True)
    op.drop_column("role", "active_code")
    op.drop_column("role", "deleted_by")
    op.drop_column("role", "deleted_at")

    op.drop_index(op.f("ix_user_active_username"), table_name="user")
    op.drop_index(op.f("ix_user_active_feishu_open_id"), table_name="user")
    op.drop_index(op.f("ix_user_deleted_at"), table_name="user")
    _null_deleted_duplicate_values(
        op.get_bind(),
        table_name="user",
        column_name="feishu_open_id",
    )
    _null_deleted_duplicate_values(
        op.get_bind(),
        table_name="user",
        column_name="username",
    )
    op.create_index(
        op.f("ix_user_feishu_open_id"),
        "user",
        ["feishu_open_id"],
        unique=True,
    )
    op.create_index(op.f("ix_user_username"), "user", ["username"], unique=True)
    op.drop_column("user", "active_username")
    op.drop_column("user", "active_feishu_open_id")
    op.drop_column("user", "deleted_by")
    op.drop_column("user", "deleted_at")
