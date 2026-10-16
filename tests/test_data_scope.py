import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.models.role import DataScope
from app.models.user import User, UserStatus
from app.schemas.department import DepartmentCreate
from app.schemas.user import UserCreate
from app.services.department import create_department
from app.services.user import create_local_user

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


@dataclass(frozen=True)
class ScopeData:
    prefix: str
    company: Department
    engineering: Department
    frontend: Department
    marketing: Department
    pending: Department
    users: dict[str, User]

    def username(self, label: str) -> str:
        return f"{self.prefix}-{label}"

    def context(
        self,
        *,
        label: str = "alice",
        data_scope: DataScope | None,
        department_id: int | None,
        custom_department_ids: frozenset[int] = frozenset(),
        include_children: bool = True,
        role_id: int | None = 7001,
        is_super_admin: bool = False,
    ) -> AuthContext:
        return AuthContext(
            user_id=self.users[label].id,
            role_id=role_id,
            department_id=department_id,
            data_scope=data_scope,
            custom_department_ids=custom_department_ids,
            include_children=include_children,
            permission_codes=frozenset(),
            is_super_admin=is_super_admin,
        )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def scope_data(session: AsyncSession) -> ScopeData:
    prefix = f"scope-{uuid4().hex[:10]}"
    company = await create_department(session, DepartmentCreate(name="总公司"))
    engineering = await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=company.id),
    )
    frontend = await create_department(
        session,
        DepartmentCreate(name="前端组", parent_id=engineering.id),
    )
    marketing = await create_department(
        session,
        DepartmentCreate(name="市场部", parent_id=company.id),
    )

    pending = Department(
        parent_id=None,
        name="待分配",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    session.add(pending)
    await session.flush()
    pending.path = f"/{pending.id}/"
    await session.commit()
    await session.refresh(pending)

    users: dict[str, User] = {}
    department_by_label = {
        "alice": engineering,
        "bob": engineering,
        "carol": frontend,
        "dave": marketing,
        "pending": pending,
    }
    for label, department in department_by_label.items():
        user = await create_local_user(
            session,
            UserCreate(username=f"{prefix}-{label}", password="password"),
        )
        user.department_id = department.id
        await session.commit()
        await session.refresh(user)
        users[label] = user

    return ScopeData(
        prefix=prefix,
        company=company,
        engineering=engineering,
        frontend=frontend,
        marketing=marketing,
        pending=pending,
        users=users,
    )


async def _request(
    session: AsyncSession,
    context: AuthContext | None,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Response:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    if context is not None:

        async def override_context() -> AuthContext:
            return context

        application.dependency_overrides[get_current_context] = override_context

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                path,
                params=params,
                json=json_body,
            )
    finally:
        application.dependency_overrides.clear()


async def _list_usernames(
    session: AsyncSession,
    scope_data: ScopeData,
    context: AuthContext,
) -> set[str]:
    response = await _request(
        session,
        context,
        "GET",
        "/api/v1/users",
        params={"keyword": scope_data.prefix, "page_size": 100},
    )
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    return {item["username"] for item in items}


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (DataScope.ALL, {"alice", "bob", "carol", "dave", "pending"}),
        (DataScope.DEPT, {"alice", "bob"}),
        (DataScope.DEPT_AND_CHILD, {"alice", "bob", "carol"}),
        (DataScope.SELF, {"alice"}),
    ],
)
async def test_user_list_respects_data_scope(
    session: AsyncSession,
    scope_data: ScopeData,
    scope: DataScope,
    expected: set[str],
) -> None:
    context = scope_data.context(
        data_scope=scope,
        department_id=scope_data.engineering.id,
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == {scope_data.username(label) for label in expected}


async def test_custom_scope_without_children_matches_exact_departments(
    session: AsyncSession,
    scope_data: ScopeData,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.CUSTOM,
        department_id=scope_data.engineering.id,
        custom_department_ids=frozenset(
            {scope_data.engineering.id, scope_data.marketing.id}
        ),
        include_children=False,
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == {
        scope_data.username("alice"),
        scope_data.username("bob"),
        scope_data.username("dave"),
    }


async def test_custom_scope_with_children_includes_subtree(
    session: AsyncSession,
    scope_data: ScopeData,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.CUSTOM,
        department_id=scope_data.engineering.id,
        custom_department_ids=frozenset({scope_data.engineering.id}),
        include_children=True,
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == {
        scope_data.username("alice"),
        scope_data.username("bob"),
        scope_data.username("carol"),
    }


async def test_custom_scope_with_empty_department_set_returns_no_rows(
    session: AsyncSession,
    scope_data: ScopeData,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.CUSTOM,
        department_id=scope_data.engineering.id,
        custom_department_ids=frozenset(),
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == set()


@pytest.mark.parametrize(
    "scope",
    [DataScope.DEPT, DataScope.DEPT_AND_CHILD, DataScope.CUSTOM],
)
async def test_user_with_null_department_sees_nothing_for_dept_scopes(
    session: AsyncSession,
    scope_data: ScopeData,
    scope: DataScope,
) -> None:
    context = scope_data.context(
        data_scope=scope,
        department_id=None,
        custom_department_ids=frozenset({scope_data.engineering.id}),
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == set()


async def test_user_with_null_department_still_sees_self(
    session: AsyncSession,
    scope_data: ScopeData,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.SELF,
        department_id=None,
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == {scope_data.username("alice")}


@pytest.mark.parametrize(
    "scope",
    [DataScope.DEPT, DataScope.DEPT_AND_CHILD, DataScope.CUSTOM],
)
async def test_pending_assignment_department_is_not_in_any_scope(
    session: AsyncSession,
    scope_data: ScopeData,
    scope: DataScope,
) -> None:
    context = scope_data.context(
        label="pending",
        data_scope=scope,
        department_id=scope_data.pending.id,
        custom_department_ids=frozenset({scope_data.engineering.id}),
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == set()


async def test_user_with_null_role_falls_back_to_self_only(
    session: AsyncSession,
    scope_data: ScopeData,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.ALL,
        department_id=scope_data.engineering.id,
        role_id=None,
    )

    usernames = await _list_usernames(session, scope_data, context)

    assert usernames == {scope_data.username("alice")}


async def test_detail_endpoint_is_scoped_too(
    session: AsyncSession,
    scope_data: ScopeData,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.DEPT,
        department_id=scope_data.engineering.id,
    )

    visible = await _request(
        session,
        context,
        "GET",
        f"/api/v1/users/{scope_data.users['bob'].id}",
    )
    invisible = await _request(
        session,
        context,
        "GET",
        f"/api/v1/users/{scope_data.users['dave'].id}",
    )

    assert visible.status_code == 200
    assert invisible.status_code == 404
    assert invisible.json()["code"] == ErrorCode.USER_NOT_FOUND


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PATCH", "", {"nickname": "forbidden"}),
        ("DELETE", "", None),
        ("PATCH", "/role", {"role_id": None}),
        ("PATCH", "/department", {"department_id": None}),
        ("PATCH", "/status", {"status": "disabled"}),
        ("POST", "/password/reset", {"new_password": "new-password"}),
    ],
)
async def test_write_on_invisible_user_returns_403(
    session: AsyncSession,
    scope_data: ScopeData,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
) -> None:
    context = scope_data.context(
        data_scope=DataScope.DEPT,
        department_id=scope_data.engineering.id,
    )
    target = scope_data.users["dave"]
    original_password_hash = target.password_hash

    response = await _request(
        session,
        context,
        method,
        f"/api/v1/users/{target.id}{suffix}",
        json_body=body,
    )

    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.FORBIDDEN

    await session.refresh(target)
    assert target.nickname is None
    assert target.role_id is None
    assert target.department_id == scope_data.marketing.id
    assert target.status == UserStatus.ACTIVE
    assert target.password_hash == original_password_hash


async def test_users_endpoint_default_context_is_unauthenticated(
    session: AsyncSession,
) -> None:
    response = await _request(session, None, "GET", "/api/v1/users")

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.UNAUTHENTICATED
