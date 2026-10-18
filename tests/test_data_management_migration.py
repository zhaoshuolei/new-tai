import importlib
import re
from collections.abc import Callable
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_MODULE = "migrations.versions.4d8e1f2a7c90_custom_data_management"

TABLES = (
    "custom_data_table",
    "custom_data_field",
    "custom_data_enum_option",
    "custom_data_import",
    "custom_data_import_error",
    "custom_data_snapshot",
    "custom_data_current_snapshot",
    "custom_data_record",
    "custom_data_record_value",
)

_DML = re.compile(r"^\s*(?:INSERT\s+INTO|UPDATE\b|DELETE\s+FROM)\b", re.IGNORECASE)


def _capture(operation: Callable[[], None]) -> list[str]:
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


def _created_tables(statements: list[str]) -> list[str]:
    result: list[str] = []
    pattern = re.compile(r"CREATE\s+TABLE\s+`?([a-z0-9_]+)`?", re.IGNORECASE)
    for statement in statements:
        match = pattern.match(statement)
        if match is not None:
            result.append(match.group(1))
    return result


def _dropped_tables(statements: list[str]) -> list[str]:
    result: list[str] = []
    pattern = re.compile(r"DROP\s+TABLE\s+`?([a-z0-9_]+)`?", re.IGNORECASE)
    for statement in statements:
        match = pattern.match(statement)
        if match is not None:
            result.append(match.group(1))
    return result


def _create_statement(statements: list[str], table_name: str) -> str:
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+`?{re.escape(table_name)}`?(?:\s|$)",
        re.IGNORECASE,
    )
    matches = [statement for statement in statements if pattern.match(statement)]
    assert len(matches) == 1
    return matches[0]


def _dml(statements: list[str]) -> list[str]:
    return [statement for statement in statements if _DML.match(statement)]


def test_revision_follows_task_one_and_is_a_single_linear_successor() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    assert migration.revision == "4d8e1f2a7c90"
    assert migration.down_revision == "a7d3c9e15b42"
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_upgrade_creates_tables_in_dependency_order_without_business_dml() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)

    statements = _capture(migration.upgrade)

    assert _created_tables(statements) == list(TABLES)
    assert _dml(statements) == []


def test_upgrade_uses_generated_system_key_uniqueness_and_required_indexes() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    statements = _capture(migration.upgrade)

    field_table = _create_statement(statements, "custom_data_field")
    assert (
        "system_key_unique VARCHAR(32) GENERATED ALWAYS AS "
        "(CASE WHEN system_key = 'CUSTOM' THEN NULL ELSE system_key END) STORED"
        in field_table
    )
    assert "uq_custom_data_field_table_system_key" in " ".join(statements)
    assert "uq_custom_data_field_table_label" in " ".join(statements)

    joined = " ".join(statements)
    for index_name in (
        "ix_custom_data_record_snapshot_id",
        "ix_custom_data_record_table_date",
        "ix_custom_data_record_upstream_customer_id",
        "ix_custom_data_record_downstream_customer_id",
        "ix_custom_data_record_value_record_id",
        "ix_custom_data_record_value_field_id",
        "ix_custom_data_record_value_record_text",
        "ix_custom_data_record_value_record_integer",
        "ix_custom_data_record_value_record_decimal",
        "ix_custom_data_record_value_record_date",
        "ix_custom_data_record_value_record_boolean",
        "ix_custom_data_record_value_record_enum",
    ):
        assert index_name in joined


def test_upgrade_enforces_single_active_snapshot_per_table_and_date() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    statements = _capture(migration.upgrade)

    snapshot_table = _create_statement(statements, "custom_data_snapshot")
    assert (
        "active_business_key DATE GENERATED ALWAYS AS "
        "(CASE WHEN status = 'ACTIVE' THEN business_date END) STORED" in snapshot_table
    )
    joined = " ".join(statements)
    assert (
        "CREATE UNIQUE INDEX uq_custom_data_snapshot_table_active_date "
        "ON custom_data_snapshot (table_id, active_business_key)"
    ) in joined
    assert (
        "CONSTRAINT uq_custom_data_snapshot_table_date_version UNIQUE "
        "(table_id, business_date, version)"
    ) in snapshot_table


def test_enum_option_has_no_redundant_field_id_index() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    statements = _capture(migration.upgrade)

    enum_option = _create_statement(statements, "custom_data_enum_option")
    assert (
        "CONSTRAINT uq_custom_data_enum_option_field_label UNIQUE (field_id, label)"
        in enum_option
    )
    assert not any(
        statement.startswith("CREATE INDEX ix_custom_data_enum_option_field_id")
        or statement.startswith(
            "CREATE UNIQUE INDEX ix_custom_data_enum_option_field_id"
        )
        for statement in statements
    )


def test_upgrade_constraints_match_cleanup_and_restriction_boundaries() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    statements = _capture(migration.upgrade)

    field = _create_statement(statements, "custom_data_field")
    assert "FOREIGN KEY(table_id)" in field
    assert "ON DELETE CASCADE" in field

    enum_option = _create_statement(statements, "custom_data_enum_option")
    assert "ON DELETE CASCADE" in enum_option

    job = _create_statement(statements, "custom_data_import")
    assert "ON DELETE CASCADE" in job
    assert "ON DELETE RESTRICT" in job

    error = _create_statement(statements, "custom_data_import_error")
    assert "ON DELETE CASCADE" in error
    assert "ON DELETE RESTRICT" in error

    snapshot = _create_statement(statements, "custom_data_snapshot")
    assert "ON DELETE CASCADE" in snapshot
    assert "ON DELETE RESTRICT" in snapshot

    current = _create_statement(statements, "custom_data_current_snapshot")
    assert "ON DELETE CASCADE" in current
    assert "ON DELETE RESTRICT" in current
    assert "PRIMARY KEY (table_id, business_date)" in current

    record = _create_statement(statements, "custom_data_record")
    assert record.count("ON DELETE CASCADE") == 2
    assert record.count("ON DELETE RESTRICT") == 2

    value = _create_statement(statements, "custom_data_record_value")
    assert "ON DELETE CASCADE" in value
    assert value.count("ON DELETE RESTRICT") == 2


def test_upgrade_uses_decimal_30_8_for_all_business_amounts() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    statements = _capture(migration.upgrade)

    record = _create_statement(statements, "custom_data_record")
    value = _create_statement(statements, "custom_data_record_value")
    assert record.count("DECIMAL(30, 8)") == 2
    assert value.count("DECIMAL(30, 8)") == 1


def test_downgrade_drops_tables_in_reverse_order_without_business_dml() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)

    statements = _capture(migration.downgrade)

    assert _dropped_tables(statements) == list(reversed(TABLES))
    assert _dml(statements) == []
