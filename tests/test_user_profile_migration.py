"""Structural guard for the user profile migration.

The spec requires that upgrading and downgrading never rewrites existing
``user`` rows. Running Alembic against a scratch MySQL database would be slow
and intrusive, so this test captures the SQL the migration emits in offline
mode and asserts that no statement ever targets the ``user`` table.
"""

import importlib
import re
from io import StringIO
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_MODULE = "migrations.versions.26a11cef913c_create_user_profile"

# A statement that would mutate ``user``. ``REFERENCES user (id)`` inside the
# foreign key is a read-side dependency and deliberately not matched.
_USER_WRITE = re.compile(
    r"^\s*(?:ALTER\s+TABLE\s+`?user`?\b"
    r"|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?`?user`?\b"
    r"|CREATE\s+TABLE\s+`?user`?\b"
    r"|UPDATE\s+`?user`?\b"
    r"|INSERT\s+INTO\s+`?user`?\b"
    r"|DELETE\s+FROM\s+`?user`?\b)",
    re.IGNORECASE,
)


def _capture(operation: Any) -> list[str]:
    buffer = StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={
            "as_sql": True,
            "output_buffer": buffer,
        },
    )
    with Operations.context(context):
        operation()
    return [
        statement.strip()
        for statement in buffer.getvalue().split(";")
        if statement.strip()
    ]


def _user_writes(statements: list[str]) -> list[str]:
    return [statement for statement in statements if _USER_WRITE.match(statement)]


def test_upgrade_creates_profile_table_without_writing_user_rows() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)

    statements = _capture(migration.upgrade)

    created = [
        statement
        for statement in statements
        if re.match(r"CREATE\s+TABLE\s+`?user_profile`?\b", statement, re.I)
    ]
    assert len(created) == 1
    assert _user_writes(statements) == []


def test_downgrade_drops_profile_table_without_writing_user_rows() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)

    statements = _capture(migration.downgrade)

    dropped = [
        statement
        for statement in statements
        if re.match(r"DROP\s+TABLE\s+`?user_profile`?\b", statement, re.I)
    ]
    assert len(dropped) == 1
    assert _user_writes(statements) == []
