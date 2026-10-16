"""客户与天眼查集成接口测试。

全部离线：天眼查客户端以替身注入，绝不发起真实网络调用（无真实 token）。
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.clients.tianyancha import Candidate, RegistrationInfo
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.customer import Customer, CustomerSource
from app.models.role import DataScope
from app.models.user import User, UserStatus
from app.services import audit as audit_service
from app.services import customer as customer_service
from tests.conftest import FakeRedis

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

REQUESTED_CREDIT_CODE = "911100007693890511"


class FakeTianyanchaClient:
    """天眼查客户端替身，实现服务层用到的三个方法。"""

    def __init__(self) -> None:
        self.pages: dict[int, list[Candidate]] = {}
        self.total = 0
        self.search_calls = 0
        self.registration_calls = 0
        self.registration = RegistrationInfo(
            name="乐视网信息技术（北京）股份有限公司",
            credit_code=REQUESTED_CREDIT_CODE,
            legal_person="刘延峰",
            registered_capital="398944.0192万人民币",
            established_date="2004-11-30",
            business_scope="互联网信息服务",
            reg_status="存续",
        )
        self.registration_error: Exception | None = None

    async def search_candidates(
        self, keyword: str, *, page: int, page_size: int
    ) -> tuple[list[Candidate], int]:
        self.search_calls += 1
        return self.pages.get(page, []), self.total

    async def fetch_registration(self, credit_code: str) -> RegistrationInfo:
        self.registration_calls += 1
        if self.registration_error is not None:
            raise self.registration_error
        return self.registration

    async def fetch_historical_registration_text(self, credit_code: str) -> str:
        return ""


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


@pytest_asyncio.fixture(autouse=True)
async def audit_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(AuditLog))
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@pytest_asyncio.fixture
async def caller(session: AsyncSession) -> User:
    user = User(
        username=f"api-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
def fake_client(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeTianyanchaClient:
    client = FakeTianyanchaClient()

    async def _get_client(_session: AsyncSession) -> FakeTianyanchaClient:
        return client

    monkeypatch.setattr(customer_service, "get_tianyancha_client", _get_client)
    return client


def _context(
    caller: User,
    *permission_codes: str,
    role_id: int | None = None,
) -> AuthContext:
    return AuthContext(
        user_id=caller.id,
        role_id=role_id,
        department_id=caller.department_id,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
        username=caller.username,
    )


ALL_CUSTOMER_PERMISSIONS = (
    "customer:read",
    "customer:create",
    "customer:update",
    "customer:delete",
    "integration:read",
    "integration:update",
)


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext,
    fake_redis: FakeRedis | None = None,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return context

    redis_stub = fake_redis if fake_redis is not None else FakeRedis()
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    application.dependency_overrides[get_redis] = lambda: redis_stub
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


PROTECTED_ROUTES = [
    ("get", "/api/v1/customers", {"params": {"page": 1}}),
    ("post", "/api/v1/customers", {"json": {"name": "客户"}}),
    (
        "get",
        "/api/v1/customers/search",
        {"params": {"keyword": "乐视网"}},
    ),
    (
        "post",
        "/api/v1/customers/import",
        {"json": {"credit_code": REQUESTED_CREDIT_CODE}},
    ),
    ("get", "/api/v1/customers/1", {}),
    ("patch", "/api/v1/customers/1", {"json": {"remark": "x"}}),
    ("delete", "/api/v1/customers/1", {}),
    ("patch", "/api/v1/customers/1/owner", {"json": {"owner_user_id": 1}}),
    ("get", "/api/v1/integrations/tianyancha", {}),
    (
        "put",
        "/api/v1/integrations/tianyancha",
        {"json": {"api_key": "mcpk2_test"}},
    ),
    ("post", "/api/v1/integrations/tianyancha/test", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), PROTECTED_ROUTES)
async def test_protected_routes_require_permission(
    session: AsyncSession,
    caller: User,
    method: str,
    path: str,
    kwargs: dict[str, object],
) -> None:
    async with api_client(session, _context(caller)) as client:
        response = await getattr(client, method)(path, **kwargs)

    assert response.status_code == 403


async def test_token_response_never_contains_secret(
    session: AsyncSession,
    caller: User,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        put = await client.put(
            "/api/v1/integrations/tianyancha",
            json={"api_key": "mcpk2_secret_abc"},
        )
        assert put.status_code == 200, put.text
        assert "mcpk2_secret_abc" not in put.text
        response = await client.get("/api/v1/integrations/tianyancha")

    assert response.status_code == 200, response.text
    assert "mcpk2_secret_abc" not in response.text
    data = response.json()["data"]
    assert data["configured"] is True
    assert data["updated_by"] == caller.id


async def test_import_quota_error_maps_to_402(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
) -> None:
    fake_client.registration_error = BusinessError(
        ErrorCode.TIANYANCHA_QUOTA_EXCEEDED,
        "Tianyancha quota exceeded",
        http_status=402,
    )
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.post(
            "/api/v1/customers/import",
            json={"credit_code": REQUESTED_CREDIT_CODE},
        )

    assert response.status_code == 402
    assert response.json()["code"] == ErrorCode.TIANYANCHA_QUOTA_EXCEEDED


async def test_import_malformed_credit_code_returns_7003(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.post(
            "/api/v1/customers/import",
            json={"credit_code": "not-a-credit-code"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_search_page_size_cap_is_50(
    session: AsyncSession,
    caller: User,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网", "page_size": 51},
        )

    assert response.status_code == 422


async def test_search_route_not_shadowed_by_customer_id(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
) -> None:
    fake_client.pages = {
        1: [
            Candidate(
                credit_code=REQUESTED_CREDIT_CODE,
                name="乐视网",
                legal_person="刘延峰",
                reg_status="存续",
                company_id="1001",
            )
        ]
    }
    fake_client.total = 1
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网"},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["page_size"] == 20
    item = data["items"][0]
    assert item["name"] == "乐视网"
    assert item["existing_customer_id"] is None
    assert item["existed_hidden"] is False


async def test_list_customers_returns_paginated_envelope(
    session: AsyncSession,
    caller: User,
) -> None:
    customer = Customer(
        name="接口客户",
        credit_code=None,
        source=CustomerSource.MANUAL,
        owner_user_id=caller.id,
        owner_department_id=caller.department_id,
    )
    session.add(customer)
    await session.flush()

    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.get("/api/v1/customers")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    data = body["data"]
    assert {"items", "total", "page", "page_size"} <= set(data)
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [customer.id]


async def test_create_customer_returns_timestamps(
    session: AsyncSession,
    caller: User,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.post(
            "/api/v1/customers",
            json={"name": "接口创建客户"},
        )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["id"]
    assert data["name"] == "接口创建客户"
    assert data["created_at"] is not None
    assert data["updated_at"] is not None


async def test_import_customer_returns_timestamps(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        response = await client.post(
            "/api/v1/customers/import",
            json={"credit_code": REQUESTED_CREDIT_CODE, "short_name": "乐视网"},
        )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["id"]
    assert data["credit_code"] == REQUESTED_CREDIT_CODE
    assert data["business_scope"] == "互联网信息服务"
    assert data["created_at"] is not None
    assert data["updated_at"] is not None


async def test_update_customer_returns_timestamps(
    session: AsyncSession,
    caller: User,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        created = await client.post(
            "/api/v1/customers",
            json={"name": "接口更新客户"},
        )
        assert created.status_code == 201, created.text
        customer_id = created.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/customers/{customer_id}",
            json={"remark": "更新备注"},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["id"] == customer_id
    assert data["remark"] == "更新备注"
    assert data["created_at"] is not None
    assert data["updated_at"] is not None


async def test_transfer_owner_returns_timestamps(
    session: AsyncSession,
    caller: User,
) -> None:
    target = User(
        username=f"api-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(target)
    await session.flush()

    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS, role_id=1)
    async with api_client(session, context) as client:
        created = await client.post(
            "/api/v1/customers",
            json={"name": "接口转移客户"},
        )
        assert created.status_code == 201, created.text
        customer_id = created.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/customers/{customer_id}/owner",
            json={"owner_user_id": target.id},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["id"] == customer_id
    assert data["owner_user_id"] == target.id
    assert data["created_at"] is not None
    assert data["updated_at"] is not None


async def test_search_rate_limited_after_limit(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
    fake_redis: FakeRedis,
) -> None:
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context, fake_redis) as client:
        for _ in range(20):
            response = await client.get(
                "/api/v1/customers/search",
                params={"keyword": "乐视网"},
            )
            assert response.status_code == 200, response.text

        calls_before_rejected = fake_client.search_calls
        rejected = await client.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网"},
        )

    assert rejected.status_code == 429
    assert rejected.json()["code"] == ErrorCode.RATE_LIMITED
    assert fake_client.search_calls == calls_before_rejected


async def test_import_rate_limited_after_limit(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
    fake_redis: FakeRedis,
) -> None:
    # 让上游在每次被调用时都报错，避免真正的导入落库掩盖限流计数；
    # 被限流的第 11 次请求必须在触达上游之前被拒绝。
    fake_client.registration_error = BusinessError(
        ErrorCode.TIANYANCHA_UNAVAILABLE,
        "Tianyancha unavailable",
        http_status=502,
    )
    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context, fake_redis) as client:
        for _ in range(10):
            await client.post(
                "/api/v1/customers/import",
                json={"credit_code": REQUESTED_CREDIT_CODE},
            )

        calls_before_rejected = fake_client.registration_calls
        rejected = await client.post(
            "/api/v1/customers/import",
            json={"credit_code": REQUESTED_CREDIT_CODE},
        )

    assert calls_before_rejected == 10
    assert rejected.status_code == 429
    assert rejected.json()["code"] == ErrorCode.RATE_LIMITED
    assert fake_client.registration_calls == calls_before_rejected


async def test_rate_limit_is_per_user(
    session: AsyncSession,
    caller: User,
    fake_client: FakeTianyanchaClient,
    fake_redis: FakeRedis,
) -> None:
    other = User(
        username=f"api-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(other)
    await session.flush()

    context_a = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    context_b = _context(other, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context_a, fake_redis) as client_a:
        for _ in range(20):
            response = await client_a.get(
                "/api/v1/customers/search",
                params={"keyword": "乐视网"},
            )
            assert response.status_code == 200, response.text
        rejected = await client_a.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网"},
        )
        assert rejected.status_code == 429

    async with api_client(session, context_b, fake_redis) as client_b:
        response = await client_b.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网"},
        )

    assert response.status_code == 200, response.text
