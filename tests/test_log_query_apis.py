from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.db.session import session_factory
from app.main import create_app
from app.models.audit_log import AuditLog, AuditResult
from app.models.login_log import LoginLog, LoginResultEnum, LoginType
from app.models.role import DataScope


def context_with_permissions(*permission_codes: str) -> AuthContext:
    return AuthContext(
        user_id=1,
        role_id=1,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
    )


def client_with_permissions(*permission_codes: str) -> AsyncClient:
    application = create_app()

    async def override_context() -> AuthContext:
        return context_with_permissions(*permission_codes)

    application.dependency_overrides[get_current_context] = override_context
    return AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def seeded_logs() -> AsyncIterator[dict[str, Any]]:
    marker = uuid4().hex[:12]
    base = datetime(2050, 1, 1)
    resource_type = f"task11-{marker}"
    primary_action = f"task11:{marker}:update"
    other_action = f"task11:{marker}:delete"
    matched_nickname = f"nickname-match-{marker}"
    username = f"login-{marker}"
    missing_username = f"missing-{marker}"
    missing_resource_type = f"missing-{marker}"

    audit_rows = [
        AuditLog(
            user_id=101,
            actor_username=f"alice-{marker}",
            actor_nickname=f"audit-{marker}",
            action=primary_action,
            resource_type=resource_type,
            resource_id=1101,
            result=AuditResult.SUCCESS,
            method="PATCH",
            path=f"/api/v1/task11/{marker}/1101",
            ip="203.0.113.21",
            request_id=f"task11-{marker}-audit-1",
            created_at=base,
        ),
        AuditLog(
            user_id=102,
            actor_username=f"bob-{marker}",
            actor_nickname=matched_nickname,
            action=primary_action,
            resource_type=resource_type,
            resource_id=1102,
            result=AuditResult.DENIED,
            reason="FORBIDDEN",
            method="DELETE",
            path=f"/api/v1/task11/{marker}/1102",
            ip="203.0.113.22",
            request_id=f"task11-{marker}-audit-2",
            created_at=base + timedelta(seconds=1),
        ),
        AuditLog(
            user_id=103,
            actor_username=f"carol-{marker}",
            actor_nickname=f"operator-{marker}",
            action=other_action,
            resource_type=resource_type,
            resource_id=1103,
            result=AuditResult.SUCCESS,
            method="POST",
            path=f"/api/v1/task11/{marker}/1103",
            ip="203.0.113.23",
            request_id=f"task11-{marker}-audit-3",
            created_at=base + timedelta(seconds=2),
        ),
    ]
    login_rows = [
        LoginLog(
            user_id=201,
            username=username,
            login_type=LoginType.LOCAL,
            result=LoginResultEnum.SUCCESS,
            ip="203.0.113.31",
            request_id=f"task11-{marker}-login-1",
            created_at=base,
        ),
        LoginLog(
            user_id=None,
            username=username,
            login_type=LoginType.FEISHU,
            result=LoginResultEnum.FAILURE,
            reason="FEISHU_UPSTREAM_ERROR",
            ip="203.0.113.32",
            request_id=f"task11-{marker}-login-2",
            created_at=base + timedelta(seconds=1),
        ),
    ]

    audit_ids: list[int] = []
    login_ids: list[int] = []
    async with session_factory() as session:
        session.add_all([*audit_rows, *login_rows])
        await session.commit()
        audit_ids = [row.id for row in audit_rows]
        login_ids = [row.id for row in login_rows]

    try:
        yield {
            "base": base,
            "resource_type": resource_type,
            "primary_action": primary_action,
            "matched_nickname": matched_nickname,
            "username": username,
            "missing_username": missing_username,
            "missing_resource_type": missing_resource_type,
            "audit_rows": audit_rows,
            "login_rows": login_rows,
        }
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(
                delete(AuditLog).where(AuditLog.id.in_(audit_ids))
            )
            await cleanup_session.execute(
                delete(LoginLog).where(LoginLog.id.in_(login_ids))
            )
            await cleanup_session.commit()


async def test_operations_paged_response_shape_newest_first_and_action_filter(
    seeded_logs: dict[str, Any],
) -> None:
    async with client_with_permissions("audit:read") as client:
        response = await client.get(
            "/api/v1/logs/operations",
            params={
                "action": seeded_logs["primary_action"],
                "page": 1,
                "page_size": 1,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert set(data) == {"items", "total", "page", "page_size"}
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 1
    assert [item["id"] for item in data["items"]] == [seeded_logs["audit_rows"][1].id]
    assert data["items"][0]["action"] == seeded_logs["primary_action"]
    parsed_created_at = datetime.fromisoformat(data["items"][0]["created_at"])
    assert parsed_created_at.utcoffset() == timedelta(0)


async def test_operations_result_filter(seeded_logs: dict[str, Any]) -> None:
    async with client_with_permissions("audit:read") as client:
        response = await client.get(
            "/api/v1/logs/operations",
            params={
                "resource_type": seeded_logs["resource_type"],
                "result": "denied",
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [seeded_logs["audit_rows"][1].id]
    assert data["items"][0]["result"] == "denied"


async def test_operations_time_range_filter(seeded_logs: dict[str, Any]) -> None:
    base = seeded_logs["base"]
    async with client_with_permissions("audit:read") as client:
        response = await client.get(
            "/api/v1/logs/operations",
            params={
                "resource_type": seeded_logs["resource_type"],
                "start_time": (base + timedelta(seconds=1)).isoformat(),
                "end_time": (base + timedelta(seconds=2)).isoformat(),
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert [item["id"] for item in data["items"]] == [
        seeded_logs["audit_rows"][2].id,
        seeded_logs["audit_rows"][1].id,
    ]


async def test_operations_keyword_matches_actor_nickname(
    seeded_logs: dict[str, Any],
) -> None:
    async with client_with_permissions("audit:read") as client:
        response = await client.get(
            "/api/v1/logs/operations",
            params={
                "resource_type": seeded_logs["resource_type"],
                "keyword": seeded_logs["matched_nickname"],
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [seeded_logs["audit_rows"][1].id]


async def test_login_logs_paged_response_shape(
    seeded_logs: dict[str, Any],
) -> None:
    async with client_with_permissions("login_log:read") as client:
        response = await client.get(
            "/api/v1/logs/logins",
            params={"username": seeded_logs["username"]},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"items", "total", "page", "page_size"}
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert [item["id"] for item in data["items"]] == [
        seeded_logs["login_rows"][1].id,
        seeded_logs["login_rows"][0].id,
    ]
    assert data["items"][0]["login_type"] == "feishu"
    assert datetime.fromisoformat(data["items"][0]["created_at"]).utcoffset() == (
        timedelta(0)
    )


async def test_log_endpoints_reject_missing_permission(
    seeded_logs: dict[str, Any],
) -> None:
    async with client_with_permissions() as client:
        operations_response = await client.get("/api/v1/logs/operations")
        logins_response = await client.get("/api/v1/logs/logins")

    assert operations_response.status_code == 403
    assert logins_response.status_code == 403


async def test_empty_log_results_return_empty_page(
    seeded_logs: dict[str, Any],
) -> None:
    async with client_with_permissions("audit:read", "login_log:read") as client:
        operations_response = await client.get(
            "/api/v1/logs/operations",
            params={"resource_type": seeded_logs["missing_resource_type"]},
        )
        logins_response = await client.get(
            "/api/v1/logs/logins",
            params={"username": seeded_logs["missing_username"]},
        )

    assert operations_response.status_code == 200
    operations_data = operations_response.json()["data"]
    assert operations_data["items"] == []
    assert operations_data["total"] == 0

    assert logins_response.status_code == 200
    logins_data = logins_response.json()["data"]
    assert logins_data["items"] == []
    assert logins_data["total"] == 0
