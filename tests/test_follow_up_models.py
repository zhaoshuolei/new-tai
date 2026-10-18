from typing import cast

from sqlalchemy import Computed, String, Table, inspect

from app.models.follow_up import (
    ContactFollowUp,
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)


def test_method_columns_and_generated_name() -> None:
    table = cast(Table, ContactFollowUpMethod.__table__)
    columns = {c.name for c in inspect(ContactFollowUpMethod).columns}
    assert {"name", "status", "active_name", "deleted_at", "deleted_by"} <= columns
    computed = table.c.active_name.computed
    assert isinstance(computed, Computed)
    assert "status = 'ACTIVE'" in str(computed.sqltext)
    unique_columns = {
        tuple(c.name for c in index.columns) for index in table.indexes if index.unique
    }
    assert ("active_name",) in unique_columns


def test_follow_up_columns() -> None:
    table = cast(Table, ContactFollowUp.__table__)
    columns = {c.name for c in inspect(ContactFollowUp).columns}
    assert {
        "contact_id",
        "method_id",
        "follow_up_user_id",
        "follow_up_at",
        "content",
        "next_follow_up_at",
        "status",
        "created_by",
    } <= columns
    assert table.c.content.nullable is False
    assert table.c.follow_up_at.nullable is False
    assert table.c.next_follow_up_at.nullable is True


def test_follow_up_indexes_exist() -> None:
    table = cast(Table, ContactFollowUp.__table__)
    index_names = {index.name for index in table.indexes}
    assert "ix_contact_follow_up_contact_status_time" in index_names
    assert "ix_contact_follow_up_status_next" in index_names


def test_attachment_columns() -> None:
    table = cast(Table, ContactFollowUpAttachment.__table__)
    columns = {c.name for c in inspect(ContactFollowUpAttachment).columns}
    assert {
        "follow_up_id",
        "storage_path",
        "original_name",
        "content_type",
        "size_bytes",
        "created_by",
    } <= columns
    # DOCX 的 MIME 类型长 71 字符，64 存不下。
    content_type = table.c.content_type.type
    assert isinstance(content_type, String)
    assert content_type.length is not None
    assert content_type.length >= 71
