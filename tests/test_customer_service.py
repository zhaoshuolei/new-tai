"""客户服务测试（检索、回填、CRUD、负责人转移）。

全部离线：天眼查客户端以替身注入，绝不发起真实网络调用（无真实 token）。
"""

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.clients.tianyancha import Candidate, RegistrationInfo
from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.customer import Customer, CustomerSource
from app.models.department import Department, DepartmentStatus
from app.models.role import DataScope
from app.models.user import User, UserStatus
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.services import customer as customer_service
from app.services.customer import (
    create_customer,
    delete_customer,
    get_customer,
    import_from_tianyancha,
    search_candidates,
    transfer_owner,
    update_customer,
)

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

HISTORICAL_WITHOUT_MATCH = """
##### 经营范围 2022-03-11
###### 变更后原文
完全不相干的经营范围
"""


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
    """天眼查客户端替身，实现 Task 7 的三个方法，记录调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pages: dict[int, list[Candidate]] = {}
        self.total = 0
        self.registration = RegistrationInfo(
            name="乐视网信息技术（北京）股份有限公司",
            credit_code=REQUESTED_CREDIT_CODE,
            legal_person="刘延峰",
            registered_capital="398944.0192万人民币",
            established_date="2004-11-30",
            business_scope=TRUNCATED_SCOPE,
            reg_status="存续",
        )
        self.registration_credit_code: str | None = None
        self.historical_text = HISTORICAL_WITH_MATCH

    async def search_candidates(
        self, keyword: str, *, page: int, page_size: int
    ) -> tuple[list[Candidate], int]:
        self.calls.append(f"search:{page}:{page_size}")
        return self.pages.get(page, []), self.total

    async def fetch_registration(self, credit_code: str) -> RegistrationInfo:
        self.calls.append("registration")
        code = self.registration_credit_code or self.registration.credit_code
        return replace(self.registration, credit_code=code)

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
    data_scope: DataScope = DataScope.ALL,
    is_super_admin: bool = False,
    role_id: int | None = 1,
) -> AuthContext:
    return AuthContext(
        user_id=user.id,
        role_id=role_id,
        department_id=user.department_id,
        data_scope=data_scope,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(),
        is_super_admin=is_super_admin,
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


async def count_customers(session: AsyncSession) -> int:
    total = await session.scalar(select(func.count()).select_from(Customer))
    return int(total or 0)


async def test_import_rejects_malformed_credit_code_without_calling_client(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, "bad", None, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD
    assert exc.value.http_status == 422
    assert fake_client.calls == []


async def test_import_rejects_existing_visible_without_calling_client(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    existing = await make_customer_row(
        session,
        owner=caller,
        credit_code=REQUESTED_CREDIT_CODE,
    )

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(
            session,
            REQUESTED_CREDIT_CODE,
            None,
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409
    assert exc.value.extra == {"existing_customer_id": existing.id}
    assert fake_client.calls == []


async def test_import_existing_hidden_omits_existing_id(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    await make_customer_row(
        session,
        owner=owner,
        credit_code=REQUESTED_CREDIT_CODE,
    )
    viewer = await make_user(session)
    context = make_context(viewer, data_scope=DataScope.SELF)

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(
            session,
            REQUESTED_CREDIT_CODE,
            None,
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.extra is None
    assert fake_client.calls == []


async def test_import_refuses_when_upstream_subject_differs(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    fake_client.registration_credit_code = "91110000000000000X"

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(
            session,
            REQUESTED_CREDIT_CODE,
            None,
            context=context,
        )

    assert exc.value.code == ErrorCode.TIANYANCHA_MALFORMED_RESPONSE
    assert exc.value.http_status == 502
    assert await count_customers(session) == 0


async def test_import_success_maps_fields_and_resolves_scope(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    department = await make_department(session)
    caller = await make_user(session, department=department)
    context = make_context(caller)

    customer = await import_from_tianyancha(
        session,
        f"  {REQUESTED_CREDIT_CODE.lower()} ",
        "乐视网",
        context=context,
    )

    assert customer.source == CustomerSource.TIANYANCHA
    assert customer.credit_code == REQUESTED_CREDIT_CODE
    assert customer.name == "乐视网信息技术（北京）股份有限公司"
    assert customer.short_name == "乐视网"
    assert customer.legal_person == "刘延峰"
    assert customer.registered_capital == "398944.0192万人民币"
    assert customer.established_date == date(2004, 11, 30)
    assert customer.business_scope == FULL_SCOPE
    assert customer.business_scope_truncated is False
    assert customer.reg_status == "存续"
    assert customer.owner_user_id == context.user_id
    assert customer.owner_department_id == department.id
    assert isinstance(customer.tyc_imported_at, datetime)
    assert fake_client.calls == ["registration", "historical"]


async def test_import_without_scope_match_keeps_truncated_flag(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    fake_client.historical_text = HISTORICAL_WITHOUT_MATCH

    customer = await import_from_tianyancha(
        session,
        REQUESTED_CREDIT_CODE,
        None,
        context=context,
    )

    assert customer.business_scope == TRUNCATED_SCOPE
    assert customer.business_scope_truncated is True


async def test_search_blank_keyword_is_rejected(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)

    with pytest.raises(BusinessError) as exc:
        await search_candidates(session, "   ", page=1, page_size=20, context=context)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert exc.value.http_status == 422
    assert fake_client.calls == []


async def test_search_annotates_visible_and_skips_null_code(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    existing = await make_customer_row(
        session,
        owner=caller,
        credit_code=REQUESTED_CREDIT_CODE,
    )
    fake_client.pages = {
        1: [
            Candidate(
                credit_code=REQUESTED_CREDIT_CODE,
                name="乐视网",
                legal_person="刘延峰",
                reg_status="存续",
                company_id="1001",
            ),
            Candidate(
                credit_code=None,
                name="境外主体",
                legal_person=None,
                reg_status="仍注册",
                company_id="1002",
            ),
        ]
    }
    fake_client.total = 2

    result = await search_candidates(
        session,
        "乐视网",
        page=1,
        page_size=20,
        context=context,
    )

    assert result["total"] == 2
    assert result["page"] == 1
    assert result["page_size"] == 20
    visible, offshore = result["items"]
    assert visible["company_id"] == "1001"
    assert visible["existing_customer_id"] == existing.id
    assert "existed_hidden" not in visible
    assert "existing_customer_id" not in offshore
    assert "existed_hidden" not in offshore


async def test_search_marks_hidden_existing_without_id(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    await make_customer_row(
        session,
        owner=owner,
        credit_code=REQUESTED_CREDIT_CODE,
    )
    viewer = await make_user(session)
    context = make_context(viewer, data_scope=DataScope.SELF)
    fake_client.pages = {
        1: [
            Candidate(
                credit_code=REQUESTED_CREDIT_CODE,
                name="乐视网",
                legal_person=None,
                reg_status="存续",
                company_id="1001",
            )
        ]
    }
    fake_client.total = 1

    result = await search_candidates(
        session,
        "乐视网",
        page=1,
        page_size=20,
        context=context,
    )

    item = result["items"][0]
    assert item["existed_hidden"] is True
    assert "existing_customer_id" not in item


async def test_search_pages_do_not_overlap(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)

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

    first = await search_candidates(
        session, "乐视网", page=1, page_size=2, context=context
    )
    second = await search_candidates(
        session, "乐视网", page=2, page_size=2, context=context
    )

    ids_first = {item["company_id"] for item in first["items"]}
    ids_second = {item["company_id"] for item in second["items"]}
    assert ids_first.isdisjoint(ids_second)
    assert first["total"] == second["total"] == 4


@pytest.mark.parametrize("operation", ["get", "update", "delete"])
async def test_invisible_customer_returns_not_found(
    session: AsyncSession,
    operation: str,
) -> None:
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    viewer = await make_user(session)
    context = make_context(viewer, data_scope=DataScope.SELF)

    with pytest.raises(BusinessError) as exc:
        if operation == "get":
            await get_customer(session, customer.id, context=context)
        elif operation == "update":
            await update_customer(
                session,
                customer.id,
                CustomerUpdate(remark="x"),
                context=context,
            )
        else:
            await delete_customer(session, customer.id, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND
    assert exc.value.http_status == 404


async def test_transfer_to_missing_user_is_rejected(session: AsyncSession) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    customer = await make_customer_row(session, owner=caller)

    with pytest.raises(BusinessError) as exc:
        await transfer_owner(session, customer.id, 999999, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_OWNER_UNAVAILABLE
    assert exc.value.http_status == 422


async def test_transfer_to_disabled_user_is_rejected(session: AsyncSession) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    customer = await make_customer_row(session, owner=caller)
    disabled = await make_user(session, status=UserStatus.DISABLED)

    with pytest.raises(BusinessError) as exc:
        await transfer_owner(session, customer.id, disabled.id, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_OWNER_UNAVAILABLE


async def test_transfer_to_user_outside_scope_is_rejected(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    context = make_context(caller, data_scope=DataScope.SELF)
    customer = await make_customer_row(session, owner=caller)
    other = await make_user(session)

    with pytest.raises(BusinessError) as exc:
        await transfer_owner(session, customer.id, other.id, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_OWNER_UNAVAILABLE


async def test_transfer_resnapshots_owner_department(session: AsyncSession) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    customer = await make_customer_row(session, owner=caller)
    target_department = await make_department(session)
    target = await make_user(session, department=target_department)

    updated = await transfer_owner(
        session,
        customer.id,
        target.id,
        context=context,
    )

    assert updated.owner_user_id == target.id
    assert updated.owner_department_id == target_department.id


async def test_create_manual_customer_defaults_to_context_owner(
    session: AsyncSession,
) -> None:
    department = await make_department(session)
    caller = await make_user(session, department=department)
    context = make_context(caller)

    customer = await create_customer(
        session,
        CustomerCreate(name="手工客户"),
        context=context,
    )

    assert customer.source == CustomerSource.MANUAL
    assert customer.credit_code is None
    assert customer.owner_user_id == caller.id
    assert customer.owner_department_id == department.id


async def test_create_blank_credit_code_becomes_none(session: AsyncSession) -> None:
    caller = await make_user(session)
    context = make_context(caller)

    customer = await create_customer(
        session,
        CustomerCreate(name="手工客户", credit_code="   "),
        context=context,
    )

    assert customer.credit_code is None


async def test_create_duplicate_credit_code_is_rejected(
    session: AsyncSession,
) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    existing = await create_customer(
        session,
        CustomerCreate(name="客户甲", credit_code=REQUESTED_CREDIT_CODE),
        context=context,
    )

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="客户乙", credit_code=REQUESTED_CREDIT_CODE),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.extra == {"existing_customer_id": existing.id}


async def test_create_duplicate_hidden_omits_existing_id(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    await create_customer(
        session,
        CustomerCreate(name="客户甲", credit_code=REQUESTED_CREDIT_CODE),
        context=make_context(owner),
    )
    viewer = await make_user(session)
    context = make_context(viewer, data_scope=DataScope.SELF)

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="客户乙", credit_code=REQUESTED_CREDIT_CODE),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.extra is None


async def test_update_visible_customer_changes_fields(session: AsyncSession) -> None:
    caller = await make_user(session)
    context = make_context(caller)
    customer = await make_customer_row(session, owner=caller, name="旧名称")

    updated = await update_customer(
        session,
        customer.id,
        CustomerUpdate(name="新名称", remark="备注"),
        context=context,
    )

    assert updated.name == "新名称"
    assert updated.remark == "备注"
