from datetime import datetime, timedelta

import pytest

from app.services import follow_up as follow_up_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (  # noqa: F401
    make_context,
    make_customer_row,
    make_user,
    session,
)
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)

# 固定「现在」：上海 2026-10-18 04:00 → 今日边界 = UTC 2026-10-17 16:00
NOW = datetime(2026, 10, 17, 20, 0)  # 朴素 UTC
TODAY_START = datetime(2026, 10, 17, 16, 0)
YESTERDAY_LAST = datetime(2026, 10, 17, 15, 59, 59)
TODAY_LAST = datetime(2026, 10, 18, 15, 59, 59)
TOMORROW_START = datetime(2026, 10, 18, 16, 0)


async def _setup(session, next_follow_up_at, follow_up_user=None):  # noqa: F811
    owner = follow_up_user or await make_user(session)
    customer = await make_customer_row(session, owner=owner, name="天曦科技")
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function, name="张三")
    method = await add_method(session, name="电话")
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START,
        next_follow_up_at=next_follow_up_at,
    )
    ctx = make_context(owner, permission_codes={"customer:read"})
    return owner, customer, contact, ctx


@pytest.mark.parametrize(
    ("next_follow_up_at", "expected"),
    [
        (YESTERDAY_LAST, ["overdue"]),
        (TODAY_START, ["today"]),
        (TODAY_LAST, ["today"]),
        (TOMORROW_START, []),
        (None, []),
    ],
)
async def test_due_boundaries(  # noqa: F811
    session,
    next_follow_up_at,
    expected,
    monkeypatch,
) -> None:
    placeholder = await make_user(session)  # 保证 session 已连接
    _, customer, _, ctx = await _setup(
        session,
        next_follow_up_at,
        follow_up_user=placeholder,
    )
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, total = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=None,
        page=1,
        page_size=20,
    )

    keys = [row.due_state for row in rows if row.customer.id == customer.id]
    assert keys == expected


async def test_only_latest_record_forms_current_plan(session, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, ctx = await _setup(session, YESTERDAY_LAST)
    method = await add_method(session, name="微信")
    # 更新的记录没有下次计划 → 该对接人离开清单
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START + timedelta(hours=1),
        next_follow_up_at=None,
    )
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=None,
        page=1,
        page_size=20,
    )
    assert [row.customer.id for row in rows if row.customer.id == customer.id] == []


async def test_due_filter_and_summary_counts(session, monkeypatch) -> None:  # noqa: F811
    owner = await make_user(session)
    overdue_customer = await make_customer_row(session, owner=owner, name="逾期客户")
    today_customer = await make_customer_row(session, owner=owner, name="今日客户")
    function = await add_function(session, name="商务", customer_id=None)
    method = await add_method(session, name="电话")
    overdue_contact = await add_contact(session, overdue_customer, function)
    today_contact = await add_contact(session, today_customer, function)
    await add_follow_up(
        session,
        overdue_contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START,
        next_follow_up_at=YESTERDAY_LAST,
    )
    await add_follow_up(
        session,
        today_contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START,
        next_follow_up_at=TODAY_LAST,
    )
    ctx = make_context(owner, permission_codes={"customer:read"})
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    overdue_rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="overdue",
        follow_up_user_id=None,
        page=1,
        page_size=20,
    )
    assert [row.customer.name for row in overdue_rows] == ["逾期客户"]

    summary = await follow_up_service.due_summary(
        session,
        context=ctx,
        follow_up_user_id=None,
    )
    assert summary == (1, 1)

    customer_rows, total = await follow_up_service.list_due_customers(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=None,
        page=1,
        page_size=20,
    )
    assert total == 2
    assert [
        (row.customer_name, row.due_count, row.overdue_count) for row in customer_rows
    ] == [
        ("逾期客户", 1, 1),
        ("今日客户", 1, 0),
    ]


async def test_follow_up_user_filter(session, monkeypatch) -> None:  # noqa: F811
    owner, _, _, ctx = await _setup(session, YESTERDAY_LAST)
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=owner.id,
        page=1,
        page_size=20,
    )
    assert len(rows) == 1

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=owner.id + 10_000,
        page=1,
        page_size=20,
    )
    assert rows == []


async def test_large_page_returns_empty(session, monkeypatch) -> None:  # noqa: F811
    _, _, _, ctx = await _setup(session, YESTERDAY_LAST)
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=None,
        page=10_000,
        page_size=20,
    )
    assert rows == []


async def test_scope_filter_hides_other_department(session, monkeypatch) -> None:  # noqa: F811
    from app.models.role import DataScope

    other_owner = await make_user(session)
    await _setup(session, YESTERDAY_LAST, follow_up_user=other_owner)
    viewer = await make_user(session)
    ctx = make_context(
        viewer,
        permission_codes={"customer:read"},
        data_scope=DataScope.SELF,
    )
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=None,
        page=1,
        page_size=20,
    )
    assert rows == []
