# tests/test_contact_models.py
from sqlalchemy import inspect

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
