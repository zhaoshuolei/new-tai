"""客户与集成写操作的审计装饰器测试。

审计写入使用独立 session，故即使业务 session 回滚，审计行仍可见；fixture
在前后清空 audit_log，避免污染共享表。
"""

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.clients.tianyancha import Candidate, RegistrationInfo
from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog, AuditResult
from app.models.role import DataScope
from app.models.user import User, UserStatus
from app.schemas.customer import CustomerCreate
from app.services import audit as audit_service
from app.services import customer as customer_service
from app.services.customer import (
    create_customer,
    delete_customer,
    import_from_tianyancha,
    restore_customer,
    transfer_owner,
)
from app.services.integration import save_tianyancha_token

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

REQUESTED_CREDIT_CODE = "911100007693890511"
TRUNCATED_SCOPE = "互联网信息服务..."
FULL_SCOPE = "互联网信息服务（含各类增值电信业务）完整经营范围全文。"

HISTORICAL_WITH_MATCH = f"""
##### 经营范围 2022-03-11
###### 变更前原文
旧的经营范围
###### 变更后原文
{FULL_SCOPE}
"""


@pytest_asyncio.fixture
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


class FakeTianyanchaClient:
    """天眼查客户端替身，仅实现回填所需的三个方法。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registration = RegistrationInfo(
            name="乐视网信息技术（北京）股份有限公司",
            credit_code=REQUESTED_CREDIT_CODE,
            legal_person="刘延峰",
            registered_capital="398944.0192万人民币",
            established_date="2004-11-30",
            business_scope=TRUNCATED_SCOPE,
            reg_status="存续",
        )
        self.historical_text = HISTORICAL_WITH_MATCH

    async def search_candidates(
        self, keyword: str, *, page: int, page_size: int
    ) -> tuple[list[Candidate], int]:
        self.calls.append("search")
        return [], 0

    async def fetch_registration(self, credit_code: str) -> RegistrationInfo:
        self.calls.append("registration")
        return replace(self.registration, credit_code=credit_code)

    async def fetch_historical_registration_text(self, credit_code: str) -> str:
        self.calls.append("historical")
        return self.historical_text


@pytest.fixture
def fake_client() -> FakeTianyanchaClient:
    return FakeTianyanchaClient()


@pytest.fixture(autouse=True)
def _inject_client(
    monkeypatch: pytest.MonkeyPatch, fake_client: FakeTianyanchaClient
) -> None:
    async def _get_client(_session: AsyncSession) -> FakeTianyanchaClient:
        return fake_client

    monkeypatch.setattr(customer_service, "get_tianyancha_client", _get_client)


async def make_user(
    session: AsyncSession,
    *,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=f"audit-{uuid4().hex[:12]}",
        password_hash=None,
        status=status,
        token_version=0,
        department_id=None,
    )
    session.add(user)
    await session.flush()
    return user


def make_context(user: User, **overrides: object) -> AuthContext:
    values: dict[str, object] = {
        "user_id": user.id,
        "role_id": 1,
        "department_id": user.department_id,
        "data_scope": DataScope.ALL,
        "custom_department_ids": frozenset(),
        "include_children": False,
        "permission_codes": frozenset(),
        "is_super_admin": False,
        "username": user.username,
        "nickname": user.nickname,
    }
    values.update(overrides)
    return AuthContext(**values)  # type: ignore[arg-type]


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with factory() as session:
        rows = await session.scalars(select(AuditLog).order_by(AuditLog.id))
        return list(rows.all())


async def latest_row(
    factory: async_sessionmaker[AsyncSession], action: str
) -> AuditLog:
    rows = [row for row in await audit_rows(factory) if row.action == action]
    assert len(rows) == 1
    return rows[0]


async def test_transfer_writes_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    target = await make_user(session)
    context = make_context(
        caller,
        permission_codes=frozenset({"customer:update"}),
    )
    customer = await create_customer(
        session,
        CustomerCreate(name="转移客户"),
        context=context,
    )

    await transfer_owner(
        session,
        customer.id,
        target.id,
        context=context,
    )

    row = await latest_row(audit_session_factory, "customer:transfer")
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "customer"
    assert row.resource_id == customer.id
    assert row.user_id == caller.id


async def test_save_token_audit_stores_no_secret(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    plaintext = "mcpk2_secret_value"

    await save_tianyancha_token(session, plaintext, context=context)

    row = await latest_row(audit_session_factory, "integration:update")
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "integration"
    assert row.resource_id is None
    for value in row.__dict__.values():
        assert plaintext not in str(value)


async def test_import_writes_audit_row_with_new_id(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    context = make_context(caller)

    customer = await import_from_tianyancha(
        session,
        REQUESTED_CREDIT_CODE,
        None,
        context=context,
    )

    row = await latest_row(audit_session_factory, "customer:create")
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "customer"
    assert row.resource_id == customer.id


async def test_delete_writes_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    customer = await create_customer(
        session,
        CustomerCreate(name="删除客户"),
        context=context,
    )

    await delete_customer(session, customer.id, context=context)

    row = await latest_row(audit_session_factory, "customer:delete")
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "customer"
    assert row.resource_id == customer.id


async def test_restore_writes_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    customer = await create_customer(
        session,
        CustomerCreate(name="恢复客户"),
        context=context,
    )
    await delete_customer(session, customer.id, context=context)

    await restore_customer(session, customer.id, context=context)

    row = await latest_row(audit_session_factory, "customer:restore")
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "customer"
    assert row.resource_id == customer.id


async def test_create_writes_audit_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    context = make_context(caller)

    customer = await create_customer(
        session,
        CustomerCreate(name="手工客户"),
        context=context,
    )

    row = await latest_row(audit_session_factory, "customer:create")
    assert row.result is AuditResult.SUCCESS
    assert row.resource_type == "customer"
    assert row.resource_id == customer.id


async def test_failed_operation_records_no_success_row(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caller = await make_user(session)
    disabled = await make_user(session, status=UserStatus.DISABLED)
    context = make_context(
        caller,
        permission_codes=frozenset({"customer:update"}),
    )
    customer = await create_customer(
        session,
        CustomerCreate(name="失败转移客户"),
        context=context,
    )

    with pytest.raises(BusinessError) as error:
        await transfer_owner(
            session,
            customer.id,
            disabled.id,
            context=context,
        )

    assert error.value.code == ErrorCode.CUSTOMER_OWNER_UNAVAILABLE
    assert error.value.http_status == 422
    rows = [
        row
        for row in await audit_rows(audit_session_factory)
        if row.action == "customer:transfer"
    ]
    assert rows == []
