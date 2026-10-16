"""客户管理端到端测试。

驱动真实的 FastAPI 应用（create_app + 依赖覆盖），天眼查客户端以 fake 注入，
绝不发起真实网络调用（无真实 token）。样本取自 tests/fixtures/tianyancha。
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
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

from app.clients.tianyancha import (
    Candidate,
    RegistrationInfo,
    parse_change_scope_records,
    parse_registration_table,
    parse_search_table,
)
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.customer import Customer, CustomerSource
from app.models.department import Department, DepartmentStatus
from app.models.role import DataScope
from app.models.user import User, UserStatus
from app.services import audit as audit_service
from app.services import customer as customer_service
from tests.conftest import FakeRedis

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

FIXTURES = Path(__file__).parent / "fixtures" / "tianyancha"
CREDIT_CODE = "911100007693890511"

ALL_CUSTOMER_PERMISSIONS = (
    "customer:read",
    "customer:create",
    "customer:update",
    "customer:delete",
    "integration:read",
    "integration:update",
)


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# 预期完整经营范围：历史工商 fixture 中最新一条"经营范围"变更的变更后原文。
EXPECTED_FULL_SCOPE = parse_change_scope_records(
    _read_fixture("historical_registration_excerpt.md")
)[0][1]
# 预期工商登记字段：来自真实脱敏样本，含被截断的"经营范围"。
EXPECTED_REGISTRATION = parse_registration_table(_read_fixture("registration.md"))


class FakeTianyanchaClient:
    """天眼查客户端替身：返回真实 fixture 解析出的数据，按页返回候选。"""

    def __init__(self) -> None:
        self.pages: dict[int, list[Candidate]] = {}
        self.total = 0
        self.registration = EXPECTED_REGISTRATION
        self.historical_text = _read_fixture("historical_registration_excerpt.md")

    async def search_candidates(
        self, keyword: str, *, page: int, page_size: int
    ) -> tuple[list[Candidate], int]:
        return self.pages.get(page, []), self.total

    async def fetch_registration(self, credit_code: str) -> RegistrationInfo:
        return self.registration

    async def fetch_historical_registration_text(self, credit_code: str) -> str:
        return self.historical_text


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


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeTianyanchaClient:
    client = FakeTianyanchaClient()

    async def _get_client(_session: AsyncSession) -> FakeTianyanchaClient:
        return client

    monkeypatch.setattr(customer_service, "get_tianyancha_client", _get_client)
    return client


async def make_department(session: AsyncSession) -> Department:
    department = Department(
        parent_id=None,
        name=f"部门-{uuid4().hex[:8]}",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=False,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    await session.flush()
    return department


async def make_user(
    session: AsyncSession,
    *,
    department: Department | None = None,
) -> User:
    user = User(
        username=f"e2e-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
        department_id=department.id if department is not None else None,
    )
    session.add(user)
    await session.flush()
    return user


async def make_customer(session: AsyncSession, *, owner: User) -> Customer:
    customer = Customer(
        name=f"客户-{uuid4().hex[:8]}",
        credit_code=None,
        source=CustomerSource.MANUAL,
        owner_user_id=owner.id,
        owner_department_id=owner.department_id,
    )
    session.add(customer)
    await session.flush()
    return customer


def _context(
    caller: User,
    *permission_codes: str,
    data_scope: DataScope = DataScope.ALL,
    role_id: int | None = 1,
) -> AuthContext:
    return AuthContext(
        user_id=caller.id,
        role_id=role_id,
        department_id=caller.department_id,
        data_scope=data_scope,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
        username=caller.username,
    )


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context() -> AuthContext:
        return context

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    application.dependency_overrides[get_redis] = lambda: FakeRedis()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


async def test_search_then_import_then_customer_visible_in_list(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    department = await make_department(session)
    caller = await make_user(session, department=department)
    candidates, _ = parse_search_table(_read_fixture("search_companies.md"))
    fake_client.pages = {1: [candidates[0]]}
    fake_client.total = 1

    context = _context(caller, *ALL_CUSTOMER_PERMISSIONS)
    async with api_client(session, context) as client:
        search = await client.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网"},
        )
        assert search.status_code == 200, search.text
        credit_code = search.json()["data"]["items"][0]["credit_code"]
        assert credit_code == CREDIT_CODE

        imported = await client.post(
            "/api/v1/customers/import",
            json={"credit_code": credit_code, "short_name": "乐视网"},
        )
        assert imported.status_code == 201, imported.text
        data = imported.json()["data"]
        assert data["source"] == "TIANYANCHA"
        assert data["credit_code"] == CREDIT_CODE
        assert data["business_scope"] == EXPECTED_FULL_SCOPE
        assert data["business_scope"] != EXPECTED_REGISTRATION.business_scope
        assert data["business_scope_truncated"] is False
        assert data["created_at"] is not None

        listed = await client.get(
            "/api/v1/customers",
            params={"keyword": credit_code},
        )

    assert listed.status_code == 200, listed.text
    body = listed.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["id"] == data["id"]
    assert body["items"][0]["credit_code"] == CREDIT_CODE


async def test_department_scoped_user_cannot_see_other_department_customer(
    session: AsyncSession,
) -> None:
    department_a = await make_department(session)
    department_b = await make_department(session)
    viewer = await make_user(session, department=department_a)
    owner = await make_user(session, department=department_b)
    customer = await make_customer(session, owner=owner)

    viewer_context = _context(viewer, "customer:read", data_scope=DataScope.DEPT)
    async with api_client(session, viewer_context) as client:
        detail = await client.get(f"/api/v1/customers/{customer.id}")
        assert detail.status_code == 404, detail.text
        assert detail.json()["code"] == 7001

        listed = await client.get("/api/v1/customers")

    assert listed.status_code == 200, listed.text
    body = listed.json()["data"]
    assert body["total"] == 0
    assert customer.id not in {item["id"] for item in body["items"]}

    owner_context = _context(owner, "customer:read")
    async with api_client(session, owner_context) as client:
        owner_detail = await client.get(f"/api/v1/customers/{customer.id}")

    assert owner_detail.status_code == 200, owner_detail.text
    assert owner_detail.json()["data"]["id"] == customer.id


async def test_transfer_then_original_owner_loses_visibility_under_self_scope(
    session: AsyncSession,
) -> None:
    original_owner = await make_user(session)
    new_owner = await make_user(session)
    admin = await make_user(session)
    customer = await make_customer(session, owner=original_owner)

    original_context = _context(
        original_owner,
        "customer:read",
        data_scope=DataScope.SELF,
    )
    async with api_client(session, original_context) as client:
        before = await client.get(f"/api/v1/customers/{customer.id}")
    assert before.status_code == 200, before.text

    # SELF 档位下调用者只能看到自己，无法把客户转给他人；由 ALL 档位管理员执行转移。
    admin_context = _context(admin, "customer:read", "customer:update")
    async with api_client(session, admin_context) as client:
        transferred = await client.patch(
            f"/api/v1/customers/{customer.id}/owner",
            json={"owner_user_id": new_owner.id},
        )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["data"]["owner_user_id"] == new_owner.id

    async with api_client(session, original_context) as client:
        after = await client.get(f"/api/v1/customers/{customer.id}")
    assert after.status_code == 404, after.text
    assert after.json()["code"] == 7001

    new_owner_context = _context(
        new_owner,
        "customer:read",
        data_scope=DataScope.SELF,
    )
    async with api_client(session, new_owner_context) as client:
        new_owner_view = await client.get(f"/api/v1/customers/{customer.id}")
    assert new_owner_view.status_code == 200, new_owner_view.text
    assert new_owner_view.json()["data"]["owner_user_id"] == new_owner.id


async def test_search_page_two_differs_from_page_one(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)

    def candidate(company_id: str) -> Candidate:
        return Candidate(
            credit_code=None,
            name=f"候选-{company_id}",
            legal_person=None,
            reg_status=None,
            company_id=company_id,
        )

    fake_client.pages = {
        1: [candidate("111"), candidate("222")],
        2: [candidate("333"), candidate("444")],
    }
    fake_client.total = 4

    context = _context(caller, "customer:create")
    async with api_client(session, context) as client:
        first = await client.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网", "page": 1, "page_size": 2},
        )
        second = await client.get(
            "/api/v1/customers/search",
            params={"keyword": "乐视网", "page": 2, "page_size": 2},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert first_data["total"] == second_data["total"] == 4
    ids_first = {item["company_id"] for item in first_data["items"]}
    ids_second = {item["company_id"] for item in second_data["items"]}
    assert ids_first.isdisjoint(ids_second)
    assert ids_first == {"111", "222"}
    assert ids_second == {"333", "444"}
