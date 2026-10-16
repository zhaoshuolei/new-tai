from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.db.session import session_factory
from app.main import create_app
from app.models.exception_log import ExceptionLevel, ExceptionLog
from app.models.role import DataScope
from app.services.exception_log import build_fingerprint

REQUEST_PREFIX = "test-exception-api-"


def context_with_permissions(
    *permission_codes: str,
    user_id: int = 1,
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        role_id=1,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
    )


def client_with_permissions(
    *permission_codes: str,
    user_id: int = 1,
) -> AsyncClient:
    application = create_app()

    async def override_context() -> AuthContext:
        return context_with_permissions(*permission_codes, user_id=user_id)

    application.dependency_overrides[get_current_context] = override_context
    return AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def seeded_exceptions() -> AsyncIterator[dict[str, Any]]:
    marker = uuid4().hex[:12]
    request_id = f"{REQUEST_PREFIX}{marker}"
    base = datetime(2050, 2, 1, tzinfo=UTC).replace(tzinfo=None)
    shared_type = f"Task12Shared-{marker}"
    other_type = f"Task12Other-{marker}"
    shared_fingerprint = build_fingerprint(
        exception_type=shared_type,
        message="boom",
        traceback_text="",
    )
    other_fingerprint = build_fingerprint(
        exception_type=other_type,
        message="other",
        traceback_text="",
    )

    shared_rows = [
        ExceptionLog(
            exception_type=shared_type,
            message="boom",
            fingerprint=shared_fingerprint,
            level=ExceptionLevel.ERROR,
            request_id=request_id,
            method="GET",
            path=f"/api/v1/task12/{marker}/a",
            user_id=7,
            status_code=500,
            occurred_at=base,
        ),
        ExceptionLog(
            exception_type=shared_type,
            message="boom",
            fingerprint=shared_fingerprint,
            level=ExceptionLevel.ERROR,
            request_id=request_id,
            method="GET",
            path=f"/api/v1/task12/{marker}/b",
            user_id=7,
            status_code=500,
            occurred_at=base + timedelta(seconds=1),
        ),
    ]
    other_row = ExceptionLog(
        exception_type=other_type,
        message="other",
        fingerprint=other_fingerprint,
        level=ExceptionLevel.CRITICAL,
        request_id=request_id,
        method="POST",
        path=f"/api/v1/task12/{marker}/c",
        user_id=None,
        status_code=500,
        occurred_at=base + timedelta(seconds=2),
        acknowledged=True,
        acknowledged_by=7,
        acknowledged_at=base + timedelta(seconds=3),
    )

    async with session_factory() as session:
        session.add_all([*shared_rows, other_row])
        await session.commit()
        shared_ids = [row.id for row in shared_rows]
        other_id = other_row.id

    try:
        yield {
            "base": base,
            "shared_type": shared_type,
            "other_type": other_type,
            "shared_fingerprint": shared_fingerprint,
            "other_fingerprint": other_fingerprint,
            "shared_ids": shared_ids,
            "other_id": other_id,
        }
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(
                delete(ExceptionLog).where(ExceptionLog.request_id == request_id)
            )
            await cleanup_session.commit()


async def test_summary_groups_by_fingerprint_and_includes_level(
    seeded_exceptions: dict[str, Any],
) -> None:
    async with client_with_permissions("exception:read") as client:
        response = await client.get("/api/v1/monitor/exceptions/summary")

    assert response.status_code == 200
    data = response.json()["data"]
    assert isinstance(data, list)
    group = next(
        item
        for item in data
        if item["fingerprint"] == seeded_exceptions["shared_fingerprint"]
    )
    assert group["exception_type"] == seeded_exceptions["shared_type"]
    assert group["level"] == "error"
    assert group["count"] == 2
    assert group["acknowledged"] is False
    last_occurred_at = datetime.fromisoformat(group["last_occurred_at"])
    assert last_occurred_at.utcoffset() == timedelta(0)
    assert last_occurred_at.replace(tzinfo=None) == seeded_exceptions["base"] + (
        timedelta(seconds=1)
    )


async def test_summary_acknowledged_true_when_all_rows_acked(
    seeded_exceptions: dict[str, Any],
) -> None:
    async with client_with_permissions(
        "exception:read",
        "exception:update",
    ) as client:
        for exception_id in seeded_exceptions["shared_ids"]:
            ack_response = await client.post(
                f"/api/v1/monitor/exceptions/{exception_id}/ack"
            )
            assert ack_response.status_code == 200
        response = await client.get("/api/v1/monitor/exceptions/summary")

    assert response.status_code == 200
    data = response.json()["data"]
    shared_group = next(
        item
        for item in data
        if item["fingerprint"] == seeded_exceptions["shared_fingerprint"]
    )
    other_group = next(
        item
        for item in data
        if item["fingerprint"] == seeded_exceptions["other_fingerprint"]
    )
    assert shared_group["acknowledged"] is True
    assert other_group["acknowledged"] is True


async def test_list_filters_acknowledged_false_newest_first(
    seeded_exceptions: dict[str, Any],
) -> None:
    async with client_with_permissions("exception:read") as client:
        response = await client.get(
            "/api/v1/monitor/exceptions",
            params={
                "exception_type": seeded_exceptions["shared_type"],
                "acknowledged": "false",
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"items", "total", "page", "page_size"}
    assert data["total"] == 2
    assert [item["id"] for item in data["items"]] == list(
        reversed(seeded_exceptions["shared_ids"])
    )
    assert all(item["acknowledged"] is False for item in data["items"])
    assert all(item["level"] == "error" for item in data["items"])


async def test_list_level_filter(seeded_exceptions: dict[str, Any]) -> None:
    async with client_with_permissions("exception:read") as client:
        response = await client.get(
            "/api/v1/monitor/exceptions",
            params={
                "exception_type": seeded_exceptions["other_type"],
                "level": "critical",
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [seeded_exceptions["other_id"]]


async def test_ack_sets_status_user_and_time(
    seeded_exceptions: dict[str, Any],
) -> None:
    target_id = seeded_exceptions["shared_ids"][0]
    async with client_with_permissions(
        "exception:update",
        user_id=42,
    ) as client:
        response = await client.post(f"/api/v1/monitor/exceptions/{target_id}/ack")

    assert response.status_code == 200
    row = response.json()["data"]
    assert row["id"] == target_id
    assert row["acknowledged"] is True
    assert row["acknowledged_by"] == 42
    assert row["acknowledged_at"] is not None
    assert datetime.fromisoformat(row["acknowledged_at"]).utcoffset() == timedelta(0)

    async with session_factory() as session:
        stored = await session.get(ExceptionLog, target_id)
    assert stored is not None
    assert stored.acknowledged is True
    assert stored.acknowledged_by == 42
    assert stored.acknowledged_at is not None


async def test_repeated_ack_is_idempotent(
    seeded_exceptions: dict[str, Any],
) -> None:
    target_id = seeded_exceptions["shared_ids"][0]
    async with client_with_permissions("exception:update", user_id=42) as client:
        first = await client.post(f"/api/v1/monitor/exceptions/{target_id}/ack")
    assert first.status_code == 200

    sentinel_time = datetime(2001, 1, 2, 3, 4, 5)
    async with session_factory() as session:
        stored = await session.get(ExceptionLog, target_id)
        assert stored is not None
        stored.acknowledged = True
        stored.acknowledged_by = 999
        stored.acknowledged_at = sentinel_time
        await session.commit()

    async with client_with_permissions("exception:update", user_id=77) as client:
        second = await client.post(f"/api/v1/monitor/exceptions/{target_id}/ack")

    assert second.status_code == 200
    row = second.json()["data"]
    assert row["acknowledged"] is True
    assert row["acknowledged_by"] == 999
    assert (
        datetime.fromisoformat(row["acknowledged_at"]).replace(tzinfo=None)
        == sentinel_time
    )


async def test_ack_missing_id_returns_not_found(
    seeded_exceptions: dict[str, Any],
) -> None:
    async with client_with_permissions("exception:update") as client:
        response = await client.post("/api/v1/monitor/exceptions/999999999/ack")

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.NOT_FOUND


async def test_exception_read_endpoints_require_permission(
    seeded_exceptions: dict[str, Any],
) -> None:
    async with client_with_permissions() as client:
        list_response = await client.get("/api/v1/monitor/exceptions")
        summary_response = await client.get("/api/v1/monitor/exceptions/summary")

    assert list_response.status_code == 403
    assert summary_response.status_code == 403


async def test_ack_requires_update_permission(
    seeded_exceptions: dict[str, Any],
) -> None:
    target_id = seeded_exceptions["shared_ids"][0]
    async with client_with_permissions("exception:read") as client:
        response = await client.post(f"/api/v1/monitor/exceptions/{target_id}/ack")

    assert response.status_code == 403
