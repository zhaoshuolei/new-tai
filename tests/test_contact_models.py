# tests/test_contact_models.py
from sqlalchemy import Computed, inspect

import app.models as models
from app.models.contact import ContactFunction, CustomerContact
from app.models.customer import Customer


def test_customer_has_soft_delete_columns() -> None:
    columns = {c.name for c in inspect(Customer).columns}
    assert {"status", "deleted_at", "deleted_by", "active_credit_code"} <= columns


def test_customer_credit_code_unique_is_only_on_active_column() -> None:
    unique_cols = {
        tuple(c.name for c in index.columns)
        for index in Customer.__table__.indexes
        if index.unique
    }
    assert ("active_credit_code",) in unique_cols
    assert ("credit_code",) not in unique_cols


def test_contact_function_columns() -> None:
    cols = {c.name for c in inspect(ContactFunction).columns}
    assert {"name", "customer_id", "scope_key", "status", "active_name"} <= cols


def test_customer_contact_columns() -> None:
    cols = {c.name for c in inspect(CustomerContact).columns}
    assert {
        "customer_id",
        "function_id",
        "name",
        "position",
        "phone",
        "email",
        "home_address",
        "remark",
        "status",
    } <= cols


def test_contact_generated_columns_and_defaults() -> None:
    customer_columns = Customer.__table__.c
    function_columns = ContactFunction.__table__.c
    contact_columns = CustomerContact.__table__.c

    assert isinstance(customer_columns.active_credit_code.computed, Computed)
    assert "status = 'ACTIVE'" in str(
        customer_columns.active_credit_code.computed.sqltext
    )
    assert isinstance(function_columns.active_name.computed, Computed)
    assert "status = 'ACTIVE'" in str(function_columns.active_name.computed.sqltext)
    assert customer_columns.status.server_default.arg == "ACTIVE"
    assert function_columns.status.server_default.arg == "ACTIVE"
    assert contact_columns.status.server_default.arg == "ACTIVE"
    assert customer_columns.status.nullable is False
    assert function_columns.status.nullable is False
    assert contact_columns.status.nullable is False


def test_contact_foreign_keys_restrict_deletes() -> None:
    function_columns = ContactFunction.__table__.c
    contact_columns = CustomerContact.__table__.c

    function_customer_fk = next(iter(function_columns.customer_id.foreign_keys))
    contact_customer_fk = next(iter(contact_columns.customer_id.foreign_keys))
    contact_function_fk = next(iter(contact_columns.function_id.foreign_keys))

    assert function_columns.customer_id.nullable is True
    assert function_customer_fk.target_fullname == "customer.id"
    assert function_customer_fk.ondelete == "RESTRICT"
    assert contact_columns.customer_id.nullable is False
    assert contact_columns.function_id.nullable is False
    assert contact_customer_fk.target_fullname == "customer.id"
    assert contact_customer_fk.ondelete == "RESTRICT"
    assert contact_function_fk.target_fullname == "contact_function.id"
    assert contact_function_fk.ondelete == "RESTRICT"


def test_contact_models_are_exported_consistently() -> None:
    assert models.ContactFunction is ContactFunction
    assert models.CustomerContact is CustomerContact
