from decimal import Decimal
from typing import cast

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Enum,
    ForeignKeyConstraint,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase

from app.db.base import Base
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportError,
    DataImportJob,
    DataImportSource,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
    DataTableStatus,
)


def _table(model: type[Base] | Table) -> Table:
    if isinstance(model, Table):
        return model
    return cast(Table, model.__table__)


def _unique_column_sets(table: Table) -> set[tuple[str, ...]]:
    sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    sets.update(
        tuple(column.name for column in index.columns)
        for index in table.indexes
        if index.unique
    )
    return sets


def _foreign_keys(table: Table) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for constraint in table.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        assert len(constraint.columns) == 1
        column = next(iter(constraint.columns))
        target = constraint.elements[0].target_fullname.split(".")[0]
        result[column.name] = (target, constraint.ondelete)
    return result


def _index_column_sets(table: Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in index.columns)
        for index in table.indexes
        if not index.unique
    }


def test_enum_values_are_exact_and_json_is_not_an_import_source() -> None:
    assert {item.value for item in DataTableStatus} == {"DRAFT", "LOCKED"}
    assert {item.value for item in DataFieldType} == {
        "TEXT",
        "INTEGER",
        "DECIMAL",
        "DATE",
        "BOOLEAN",
        "ENUM",
        "CUSTOMER",
    }
    assert {item.value for item in DataFieldRole} == {
        "BUSINESS_DATE",
        "UPSTREAM_CUSTOMER",
        "DOWNSTREAM_CUSTOMER",
        "REVENUE",
        "COST",
        "CUSTOM",
    }
    assert {item.value for item in DataImportSource} == {"EXCEL", "API"}
    assert {item.value for item in DataImportStatus} == {
        "QUEUED",
        "VALIDATING",
        "APPLYING",
        "SUCCEEDED",
        "FAILED",
    }
    assert {item.value for item in DataSnapshotStatus} == {
        "STAGED",
        "ACTIVE",
        "SUPERSEDED",
    }


def test_enum_columns_use_explicit_database_enum_names() -> None:
    expected = {
        DataTableDefinition: (DataTableStatus, "data_table_status"),
        DataFieldDefinition: (DataFieldType, "data_field_type"),
        DataImportJob: (DataImportStatus, "data_import_status"),
        DataSnapshot: (DataSnapshotStatus, "data_snapshot_status"),
    }
    for model, (enum_class, enum_name) in expected.items():
        table = _table(model)
        enum_columns = [
            column.type
            for column in table.columns
            if isinstance(column.type, Enum) and column.type.enum_class is enum_class
        ]
        assert enum_columns
        assert {column.name for column in enum_columns} == {enum_name}

    job_table = _table(DataImportJob)
    source = cast(Enum, job_table.c.source.type)
    assert source.enum_class is DataImportSource
    assert source.name == "data_import_source"

    field_table = _table(DataFieldDefinition)
    role = cast(Enum, field_table.c.system_key.type)
    assert role.enum_class is DataFieldRole
    assert role.name == "data_field_role"


def test_table_names_and_orm_inheritance_are_exact() -> None:
    models = (
        DataTableDefinition,
        DataFieldDefinition,
        DataEnumOption,
        DataImportJob,
        DataImportError,
        DataSnapshot,
        DataRecord,
        DataRecordValue,
    )
    assert all(issubclass(model, Base) for model in models)
    assert all(isinstance(model, type) for model in models)
    assert not isinstance(DataCurrentSnapshot, DeclarativeBase)
    assert isinstance(DataCurrentSnapshot, Table)

    assert {_table(model).name for model in models} == {
        "custom_data_table",
        "custom_data_field",
        "custom_data_enum_option",
        "custom_data_import",
        "custom_data_import_error",
        "custom_data_snapshot",
        "custom_data_record",
        "custom_data_record_value",
    }
    assert DataCurrentSnapshot.name == "custom_data_current_snapshot"


def test_table_definition_has_unique_name_and_lifecycle_columns() -> None:
    table = _table(DataTableDefinition)
    assert {
        "name",
        "description",
        "status",
        "locked_at",
        "created_by",
        "updated_by",
    } <= {column.name for column in table.columns}
    assert ("name",) in _unique_column_sets(table)
    name_type = cast(String, table.c.name.type)
    description_type = cast(String, table.c.description.type)
    assert name_type.length == 100
    assert description_type.length == 500
    assert table.c.name.nullable is False
    assert table.c.description.nullable is True
    assert table.c.locked_at.nullable is True
    assert _foreign_keys(table) == {
        "created_by": ("user", "RESTRICT"),
        "updated_by": ("user", "RESTRICT"),
    }


def test_field_system_key_is_unique_but_custom_is_repeatable() -> None:
    table = _table(DataFieldDefinition)
    computed = table.c.system_key_unique.computed
    assert computed is not None
    assert "CUSTOM" in str(computed.sqltext)

    unique_columns = {
        tuple(column.name for column in index.columns)
        for index in table.indexes
        if index.unique
    }
    assert ("table_id", "system_key_unique") in unique_columns
    assert ("table_id", "label") in unique_columns
    assert _foreign_keys(table)["table_id"] == ("custom_data_table", "CASCADE")


def test_field_definition_columns_and_types() -> None:
    table = _table(DataFieldDefinition)
    assert {
        "table_id",
        "label",
        "field_type",
        "system_key",
        "required",
        "sort_order",
        "system_key_unique",
    } <= {column.name for column in table.columns}
    assert cast(String, table.c.label.type).length == 64
    assert table.c.required.nullable is False
    assert table.c.sort_order.nullable is False
    assert table.c.system_key_unique.nullable is True


def test_enum_option_uniqueness_and_ownership() -> None:
    table = _table(DataEnumOption)
    assert ("field_id", "label") in _unique_column_sets(table)
    assert _foreign_keys(table)["field_id"] == ("custom_data_field", "CASCADE")
    assert cast(String, table.c.label.type).length == 100
    assert table.c.sort_order.nullable is False


def test_import_job_columns_fks_and_controller_source_values() -> None:
    table = _table(DataImportJob)
    assert {
        "table_id",
        "source",
        "status",
        "original_filename",
        "storage_path",
        "content_sha256",
        "size_bytes",
        "requested_by",
        "total_rows",
        "valid_rows",
        "invalid_rows",
        "date_from",
        "date_to",
        "error_count",
        "progress_percent",
        "failure_code",
        "failure_message",
        "attempt_count",
        "lease_owner",
        "lease_expires_at",
        "heartbeat_at",
        "queued_at",
        "started_at",
        "finished_at",
    } <= {column.name for column in table.columns}

    assert _foreign_keys(table) == {
        "table_id": ("custom_data_table", "CASCADE"),
        "requested_by": ("user", "RESTRICT"),
    }
    assert cast(String, table.c.original_filename.type).length == 255
    assert cast(String, table.c.storage_path.type).length == 512
    assert cast(String, table.c.content_sha256.type).length == 64
    assert isinstance(table.c.size_bytes.type, BigInteger)
    assert isinstance(table.c.date_from.type, Date)
    assert isinstance(table.c.date_to.type, Date)
    assert isinstance(table.c.lease_expires_at.type, DATETIME)
    assert isinstance(table.c.heartbeat_at.type, DATETIME)
    assert table.c.lease_expires_at.nullable is True
    assert table.c.failure_code.nullable is True
    assert table.c.failure_message.nullable is True


def test_import_error_cleanup_and_field_restriction() -> None:
    table = _table(DataImportError)
    assert {
        "import_id",
        "sheet_name",
        "row_number",
        "column_label",
        "field_id",
        "error_code",
        "message",
    } <= {column.name for column in table.columns}
    assert _foreign_keys(table) == {
        "import_id": ("custom_data_import", "CASCADE"),
        "field_id": ("custom_data_field", "RESTRICT"),
    }
    assert table.c.field_id.nullable is True


def test_snapshot_versions_are_unique_for_table_and_date() -> None:
    table = _table(DataSnapshot)
    assert ("table_id", "business_date", "version") in _unique_column_sets(table)
    assert _foreign_keys(table) == {
        "table_id": ("custom_data_table", "CASCADE"),
        "import_id": ("custom_data_import", "RESTRICT"),
    }
    assert isinstance(table.c.business_date.type, Date)
    assert table.c.row_count.nullable is False
    assert table.c.activated_at.nullable is True


def test_current_snapshot_is_a_core_pointer_table_with_composite_pk() -> None:
    table = DataCurrentSnapshot
    assert {column.name for column in table.columns} == {
        "table_id",
        "business_date",
        "snapshot_id",
        "updated_at",
    }
    assert tuple(column.name for column in table.primary_key.columns) == (
        "table_id",
        "business_date",
    )
    assert "id" not in table.c
    assert _foreign_keys(table) == {
        "table_id": ("custom_data_table", "CASCADE"),
        "snapshot_id": ("custom_data_snapshot", "RESTRICT"),
    }
    assert isinstance(table.c.updated_at.type, DATETIME)


def test_record_columns_decimal_precision_and_references() -> None:
    table = _table(DataRecord)
    assert {
        "snapshot_id",
        "table_id",
        "business_date",
        "upstream_customer_id",
        "downstream_customer_id",
        "upstream_customer_name_source",
        "downstream_customer_name_source",
        "revenue",
        "cost",
    } <= {column.name for column in table.columns}

    for column_name in ("revenue", "cost"):
        decimal_type = cast(Numeric[Decimal], table.c[column_name].type)
        assert isinstance(decimal_type, Numeric)
        assert (decimal_type.precision, decimal_type.scale) == (30, 8)
        assert table.c[column_name].nullable is False

    assert _foreign_keys(table) == {
        "snapshot_id": ("custom_data_snapshot", "CASCADE"),
        "table_id": ("custom_data_table", "CASCADE"),
        "upstream_customer_id": ("customer", "RESTRICT"),
        "downstream_customer_id": ("customer", "RESTRICT"),
    }
    assert isinstance(table.c.business_date.type, Date)
    assert table.c.business_date.nullable is False
    assert cast(String, table.c.upstream_customer_name_source.type).length == 255
    assert cast(String, table.c.downstream_customer_name_source.type).length == 255


def test_record_indexes_cover_snapshot_date_and_both_customers() -> None:
    index_columns = _index_column_sets(_table(DataRecord))
    assert ("snapshot_id",) in index_columns
    assert ("table_id", "business_date") in index_columns
    assert ("upstream_customer_id",) in index_columns
    assert ("downstream_customer_id",) in index_columns


def test_record_value_typed_columns_uniqueness_and_restrictions() -> None:
    table = _table(DataRecordValue)
    assert {
        "record_id",
        "field_id",
        "text_value",
        "integer_value",
        "decimal_value",
        "date_value",
        "boolean_value",
        "enum_option_id",
    } <= {column.name for column in table.columns}

    assert ("record_id", "field_id") in _unique_column_sets(table)
    assert _foreign_keys(table) == {
        "record_id": ("custom_data_record", "CASCADE"),
        "field_id": ("custom_data_field", "RESTRICT"),
        "enum_option_id": ("custom_data_enum_option", "RESTRICT"),
    }
    assert cast(String, table.c.text_value.type).length == 4000
    assert isinstance(table.c.integer_value.type, BigInteger)
    decimal_type = cast(Numeric[Decimal], table.c.decimal_value.type)
    assert isinstance(decimal_type, Numeric)
    assert (decimal_type.precision, decimal_type.scale) == (30, 8)
    assert isinstance(table.c.date_value.type, Date)
    assert isinstance(table.c.boolean_value.type, Boolean)
    assert all(
        table.c[column_name].nullable
        for column_name in (
            "text_value",
            "integer_value",
            "decimal_value",
            "date_value",
            "boolean_value",
            "enum_option_id",
        )
    )


def test_record_value_indexes_cover_lookup_and_each_typed_value() -> None:
    index_columns = _index_column_sets(_table(DataRecordValue))
    assert ("record_id",) in index_columns
    assert ("field_id",) in index_columns
    for value_column in (
        "text_value",
        "integer_value",
        "decimal_value",
        "date_value",
        "boolean_value",
        "enum_option_id",
    ):
        assert ("record_id", value_column) in index_columns


def test_models_register_all_tables_on_shared_metadata() -> None:
    assert {
        "custom_data_table",
        "custom_data_field",
        "custom_data_enum_option",
        "custom_data_import",
        "custom_data_import_error",
        "custom_data_snapshot",
        "custom_data_current_snapshot",
        "custom_data_record",
        "custom_data_record_value",
    } <= set(Base.metadata.tables)
