"""客户与对接人测试共享的 fixture 与构造函数。

签名在此固定，其他测试只能从本模块导入，不得各自复制。
全部离线：天眼查客户端以替身注入。
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)

from app.clients.tianyancha import Candidate, RegistrationInfo
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.customer import Customer, CustomerSource
from app.models.department import Department, DepartmentStatus
from app.models.role import DataScope
from app.models.user import User, UserStatus

TEST_DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)
REQUESTED_CREDIT_CODE = "911100007693890511"


class FakeTianyanchaClient:
    """天眼查客户端替身，记录调用，返回固定数据。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.search_calls: list[tuple[int, int]] = []
        self.pages: dict[int, list[Candidate]] = {}
        self.total = 0
        self.registration = RegistrationInfo(
            name="乐视网信息技术（北京）股份有限公司",
            credit_code=REQUESTED_CREDIT_CODE,
            legal_person="刘延峰",
            registered_capital="398944.0192万人民币",
            established_date="2004-11-30",
            business_scope="互联网信息服务...",
            reg_status="存续",
        )
        self.registration_credit_code: str | None = None
        self.historical_text = (
            "\n##### 经营范围 2022-03-11\n###### 变更前原文\n旧的经营范围\n"
            "###### 变更后原文\n"
            "互联网信息服务（含各类增值电信业务）完整经营范围全文。\n"
        )

    async def search_candidates(
        self, keyword: str, *, page: int, page_size: int
    ) -> tuple[list[Candidate], int]:
        self.calls.append(f"search:{page}:{page_size}")
        self.search_calls.append((page, page_size))
        return self.pages.get(page, []), self.total

    async def fetch_registration(self, credit_code: str) -> RegistrationInfo:
        self.calls.append("registration")
        code = self.registration_credit_code or self.registration.credit_code
        return replace(self.registration, credit_code=code)

    async def fetch_historical_registration_text(self, credit_code: str) -> str:
        self.calls.append("historical")
        return self.historical_text


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """单事务会话，测试结束回滚。写入的 commit 落在 savepoint 中。"""
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


async def make_department(
    session: AsyncSession,
    *,
    name: str | None = None,
    is_system: bool = False,
) -> Department:
    department = Department(
        parent_id=None,
        name=name or f"部门-{uuid4().hex[:8]}",
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=is_system,
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
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=f"user-{uuid4().hex[:12]}",
        password_hash=None,
        status=status,
        token_version=0,
        department_id=department.id if department is not None else None,
    )
    session.add(user)
    await session.flush()
    return user


def make_context(
    user: User,
    *,
    permission_codes: set[str] | None = None,
    data_scope: DataScope = DataScope.ALL,
    is_super_admin: bool = False,
    role_id: int | None = 1,
) -> AuthContext:
    """构造认证上下文。permission_codes 默认为空集合。"""
    return AuthContext(
        user_id=user.id,
        role_id=role_id,
        department_id=user.department_id,
        data_scope=data_scope,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(permission_codes or set()),
        is_super_admin=is_super_admin,
        username=user.username,
    )


async def make_customer_row(
    session: AsyncSession,
    *,
    owner: User,
    credit_code: str | None = None,
    name: str | None = None,
    source: CustomerSource = CustomerSource.MANUAL,
) -> Customer:
    customer = Customer(
        name=name or f"客户-{uuid4().hex[:8]}",
        credit_code=credit_code,
        source=source,
        owner_user_id=owner.id,
        owner_department_id=owner.department_id,
    )
    session.add(customer)
    await session.flush()
    return customer


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext,
    fake_redis: Any | None = None,
) -> AsyncIterator[AsyncClient]:
    """挂载应用的 HTTP 测试客户端，依赖覆盖为给定会话与上下文。

    redis 默认使用 tests/conftest.py 中的 FakeRedis；需要时可传入替身。
    """
    from tests.conftest import FakeRedis

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


__all__: tuple[str, ...] = (
    "FakeTianyanchaClient",
    "REQUESTED_CREDIT_CODE",
    "TEST_DATABASE_URL",
    "api_client",
    "make_context",
    "make_customer_row",
    "make_department",
    "make_user",
    "session",
)
