"""端到端权限流程验收：从种子数据到受限管理员操作。"""

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from scripts.init_data import init_data

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


class FakeRedis:
    """覆盖登录与刷新令牌所需的 Redis 子集。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
        get: bool = False,
    ) -> str | bool | None:
        existing = self.values.get(name)
        if nx and existing is not None:
            return None if get else False
        if xx and existing is None:
            return None if get else False
        self.values[name] = value
        if ex is not None:
            self.expirations[name] = ex
        return existing if get else True

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
                self.expirations.pop(name, None)
        return deleted

    async def scan_iter(self, *, match: str) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for name in tuple(self.values):
            if name.startswith(prefix):
                yield name

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        keys = args[:numkeys]
        values = args[numkeys:]

        if "redis.call('INCR'" in script:
            ttl = int(values[0])
            threshold = int(values[1])
            exceeded = 0
            for redis_key in keys:
                count = int(self.values.get(redis_key, "0")) + 1
                self.values[redis_key] = str(count)
                if count == 1:
                    self.expirations[redis_key] = ttl
                if count > threshold:
                    exceeded = 1
            return exceeded

        if len(keys) != 1:
            raise NotImplementedError
        redis_key = keys[0]
        current = self.values.get(redis_key)

        if "redis.call('DEL'" in script:
            expected = values[0]
            if current != expected:
                return 0
            del self.values[redis_key]
            self.expirations.pop(redis_key, None)
            return 1

        expected, replacement, ttl = values
        if current != expected:
            return 0
        self.values[redis_key] = replacement
        self.expirations[redis_key] = int(ttl)
        return 1


@pytest.fixture(autouse=True)
def strong_test_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-" + "x" * 32)


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
async def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def client(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_redis] = lambda: fake_redis

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        application.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client: AsyncClient,
    username: str,
    password: str,
    device_id: str,
) -> Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password, "device_id": device_id},
    )


async def _create_department(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str,
    parent_id: int | None = None,
) -> int:
    response = await client.post(
        "/api/v1/departments",
        headers=headers,
        json={"name": name, "parent_id": parent_id},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


async def _create_role(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    code: str,
) -> int:
    response = await client.post(
        "/api/v1/roles",
        headers=headers,
        json={
            "code": code,
            "name": code,
            "data_scope": "DEPT_AND_CHILD",
            "data_scope_include_children": True,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


async def _create_user(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    username: str,
    password: str,
) -> int:
    response = await client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


async def _assign(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    path: str,
    body: dict[str, Any],
) -> None:
    response = await client.patch(path, headers=headers, json=body)
    assert response.status_code == 200, response.text


def _find_permission_menu_id(
    nodes: list[dict[str, Any]],
    permission_code: str,
) -> int | None:
    for node in nodes:
        if node.get("permission_code") == permission_code:
            return int(node["id"])
        found = _find_permission_menu_id(node.get("children", []), permission_code)
        if found is not None:
            return found
    return None


def _collect_menu_names(nodes: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        names.add(node["name"])
        names |= _collect_menu_names(node.get("children", []))
    return names


def _visible_menu_names_for(
    nodes: list[dict[str, Any]],
    menu_id: int,
) -> set[str] | None:
    """按非按钮祖先路径推导某个已授权菜单的实际可见名称集合。

    与 `GET /me/menus` 的实现一致：只保留路径上的非按钮节点，
    因此结果完全来自真实菜单树，而不是写死的猜测。
    """
    for node in nodes:
        if int(node["id"]) == menu_id:
            return {node["name"]} if node["type"] != "BUTTON" else set()
        child_names = _visible_menu_names_for(node.get("children", []), menu_id)
        if child_names is None:
            continue
        if node["type"] == "BUTTON":
            return child_names
        return {node["name"], *child_names}
    return None


async def test_full_flow_from_seed_to_scoped_admin_operations(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """超管登录 → 建部门树 → 建角色并授权 → 建用户并分配 → 用户登录 →
    验证 /auth/me、/me/menus、/me/permissions 与受限的 /users 列表。"""
    prefix = f"e2e-{uuid4().hex[:10]}"
    superadmin_username = f"{prefix}-super"
    superadmin_password = "superadmin-password1"

    await init_data(
        session,
        superadmin_username=superadmin_username,
        superadmin_password=superadmin_password,
    )

    super_login = await _login(
        client,
        superadmin_username,
        superadmin_password,
        "e2e-super",
    )
    assert super_login.status_code == 200, super_login.text
    super_headers = _auth(super_login.json()["data"]["access_token"])

    company_id = await _create_department(
        client,
        super_headers,
        name=f"{prefix}-总公司",
    )
    engineering_id = await _create_department(
        client,
        super_headers,
        name=f"{prefix}-研发部",
        parent_id=company_id,
    )
    team_id = await _create_department(
        client,
        super_headers,
        name=f"{prefix}-前端组",
        parent_id=engineering_id,
    )
    marketing_id = await _create_department(
        client,
        super_headers,
        name=f"{prefix}-市场部",
        parent_id=company_id,
    )

    menu_response = await client.get("/api/v1/menus/tree", headers=super_headers)
    assert menu_response.status_code == 200, menu_response.text
    full_menu_tree = menu_response.json()["data"]
    user_read_menu_id = _find_permission_menu_id(
        full_menu_tree,
        "user:read",
    )
    assert user_read_menu_id is not None

    role_id = await _create_role(client, super_headers, code=f"{prefix}-role")
    assign_menus = await client.put(
        f"/api/v1/roles/{role_id}/menus",
        headers=super_headers,
        json={"menu_ids": [user_read_menu_id]},
    )
    assert assign_menus.status_code == 200, assign_menus.text

    scoped_username = f"{prefix}-scoped"
    child_username = f"{prefix}-child"
    outside_username = f"{prefix}-outside"
    scoped_password = "scoped-password1"
    scoped_id = await _create_user(
        client,
        super_headers,
        username=scoped_username,
        password=scoped_password,
    )
    child_id = await _create_user(
        client,
        super_headers,
        username=child_username,
        password="child-password1",
    )
    outside_id = await _create_user(
        client,
        super_headers,
        username=outside_username,
        password="outside-password1",
    )

    for user_id, department_id in (
        (scoped_id, engineering_id),
        (child_id, team_id),
        (outside_id, marketing_id),
    ):
        await _assign(
            client,
            super_headers,
            path=f"/api/v1/users/{user_id}/role",
            body={"role_id": role_id},
        )
        await _assign(
            client,
            super_headers,
            path=f"/api/v1/users/{user_id}/department",
            body={"department_id": department_id},
        )

    scoped_login = await _login(
        client,
        scoped_username,
        scoped_password,
        "e2e-scoped",
    )
    assert scoped_login.status_code == 200, scoped_login.text
    scoped_headers = _auth(scoped_login.json()["data"]["access_token"])

    me_response = await client.get("/api/v1/auth/me", headers=scoped_headers)
    assert me_response.status_code == 200, me_response.text
    profile = me_response.json()["data"]
    assert profile["username"] == scoped_username
    assert profile["role_id"] == role_id
    assert profile["department_id"] == engineering_id
    assert profile["data_scope"] == "DEPT_AND_CHILD"
    assert profile["include_children"] is True
    assert profile["is_super_admin"] is False
    assert profile["permission_codes"] == ["user:read"]

    permissions_response = await client.get(
        "/api/v1/me/permissions",
        headers=scoped_headers,
    )
    assert permissions_response.status_code == 200, permissions_response.text
    assert permissions_response.json()["data"] == ["user:read"]

    menus_response = await client.get(
        "/api/v1/me/menus",
        headers=scoped_headers,
    )
    assert menus_response.status_code == 200, menus_response.text
    menu_names = _collect_menu_names(menus_response.json()["data"])
    expected_menu_names = _visible_menu_names_for(full_menu_tree, user_read_menu_id)
    assert expected_menu_names == {"系统管理", "用户管理"}
    assert menu_names == expected_menu_names
    assert menu_names.isdisjoint({"角色管理", "菜单管理", "部门管理", "个人资料"})

    list_response = await client.get(
        "/api/v1/users",
        headers=scoped_headers,
        params={"keyword": prefix, "page_size": 100},
    )
    assert list_response.status_code == 200, list_response.text
    listed_usernames = {
        item["username"] for item in list_response.json()["data"]["items"]
    }
    assert listed_usernames == {scoped_username, child_username}
