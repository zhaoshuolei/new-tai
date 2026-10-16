import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.clients.feishu import FeishuClient, FeishuUserInfo
from app.core.errors import BusinessError, ErrorCode
from app.core.request_context import (
    RequestMeta,
    reset_request_meta,
    set_request_meta,
)
from app.core.security import hash_password
from app.models.login_log import LoginLog, LoginResultEnum, LoginType
from app.models.user import User, UserStatus
from app.repositories import login_log as login_log_repository
from app.services import auth as auth_service
from app.services import login_log as login_log_service

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)
REQUEST_PREFIX = "test-login-log-"


class FakeFeishuClient(FeishuClient):
    def __init__(
        self,
        *,
        open_id: str,
        error: Exception | None = None,
    ) -> None:
        self.open_id = open_id
        self.error = error

    async def exchange_code(self, code: str) -> str:
        if self.error is not None:
            raise self.error
        assert code == "valid-code"
        return "user-access-token"

    async def get_user_info(self, user_access_token: str) -> FeishuUserInfo:
        assert user_access_token == "user-access-token"
        return FeishuUserInfo(
            open_id=self.open_id,
            name="Feishu User",
            avatar_url=None,
            email=None,
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
async def login_log_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(login_log_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(
                delete(LoginLog).where(
                    LoginLog.request_id.like(f"{REQUEST_PREFIX}%"),
                )
            )
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield factory
    finally:
        await clear_rows()
        await engine.dispose()


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    meta = RequestMeta(
        request_id=request_id,
        method="POST",
        path="/api/v1/auth/login",
        ip="198.51.100.44",
        user_agent="login-log-pytest",
    )
    token = set_request_meta(meta)
    try:
        yield
    finally:
        reset_request_meta(token)


def unique_request_id() -> str:
    return f"{REQUEST_PREFIX}{uuid4().hex}"


def unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def add_local_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def login_rows(
    factory: async_sessionmaker[AsyncSession],
    *,
    request_id: str,
) -> list[LoginLog]:
    async with factory() as lookup_session:
        statement = (
            select(LoginLog)
            .where(LoginLog.request_id == request_id)
            .order_by(LoginLog.id)
        )
        return list((await lookup_session.scalars(statement)).all())


async def test_local_login_success_records_one_success_row(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-local")
    password = "correct-password1"
    user = await add_local_user(
        session,
        username=username,
        password=password,
    )
    request_id = unique_request_id()
    attempted_username = f" {username.upper()} "

    with request_context(request_id):
        result = await auth_service.login_local_user(
            session,
            fake_redis,
            username=attempted_username,
            password=password,
            client_ip="203.0.113.10",
            device_id="device-1",
        )

    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id
    assert row.username == attempted_username
    assert row.login_type is LoginType.LOCAL
    assert row.result is LoginResultEnum.SUCCESS
    assert row.reason is None
    assert row.ip == "203.0.113.10"
    assert row.user_agent == "login-log-pytest"
    assert row.device_id == "device-1"
    assert result.user.id == user.id


async def test_local_invalid_credentials_records_failure(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-invalid")
    request_id = unique_request_id()

    with request_context(request_id), pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password="wrong-password",
            client_ip="203.0.113.11",
            device_id="device-invalid",
        )

    assert error.value.code == ErrorCode.INVALID_CREDENTIALS
    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None
    assert row.username == username
    assert row.login_type is LoginType.LOCAL
    assert row.result is LoginResultEnum.FAILURE
    assert row.reason == ErrorCode.INVALID_CREDENTIALS.name


async def test_overlong_request_id_is_bounded(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-long-request-id")
    password = "correct-password1"
    await add_local_user(
        session,
        username=username,
        password=password,
    )
    request_id = f"{REQUEST_PREFIX}{'x' * 80}"

    with request_context(request_id):
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password=password,
            client_ip="203.0.113.18",
            device_id="device-long-request-id",
        )

    rows = await login_rows(login_log_factory, request_id=request_id[:64])
    assert len(rows) == 1
    assert rows[0].request_id == request_id[:64]
    assert len(rows[0].request_id) == 64


async def test_disabled_local_account_records_failure(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-disabled")
    user = await add_local_user(
        session,
        username=username,
        password="password1",
    )
    user.status = UserStatus.DISABLED
    await session.commit()
    request_id = unique_request_id()

    with request_context(request_id), pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password="password1",
            client_ip="203.0.113.12",
            device_id="device-disabled",
        )

    assert error.value.code == ErrorCode.ACCOUNT_DISABLED
    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None
    assert row.username == username
    assert row.result is LoginResultEnum.FAILURE
    assert row.reason == ErrorCode.ACCOUNT_DISABLED.name


async def test_rate_limited_local_login_records_failure(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-rate")
    await add_local_user(
        session,
        username=username,
        password="password1",
    )
    client_ip = "203.0.113.13"
    for _ in range(auth_service.LOGIN_RATE_LIMIT_ATTEMPTS):
        await auth_service.enforce_login_rate_limit(
            fake_redis,
            client_ip=client_ip,
            username=username,
        )
    request_id = unique_request_id()

    with request_context(request_id), pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password="password1",
            client_ip=client_ip,
            device_id="device-rate",
        )

    assert error.value.code == ErrorCode.RATE_LIMITED
    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None
    assert row.username == username
    assert row.result is LoginResultEnum.FAILURE
    assert row.reason == ErrorCode.RATE_LIMITED.name


async def test_feishu_login_success_records_user_without_username(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    open_id = f"ou-login-log-{uuid4().hex}"
    user = User(feishu_open_id=open_id, status=UserStatus.ACTIVE)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    request_id = unique_request_id()
    client = FakeFeishuClient(open_id=open_id)

    with request_context(request_id):
        result = await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.14",
            client=client,
        )

    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id
    assert row.username is None
    assert row.login_type is LoginType.FEISHU
    assert row.result is LoginResultEnum.SUCCESS
    assert row.reason is None
    assert row.device_id is None
    assert result.user.id == user.id


async def test_feishu_upstream_failure_records_failure(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = unique_request_id()
    upstream_error = BusinessError(
        ErrorCode.FEISHU_UPSTREAM_ERROR,
        "Feishu upstream failed",
        http_status=502,
    )
    client = FakeFeishuClient(open_id="ou-upstream", error=upstream_error)

    with request_context(request_id), pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.15",
            client=client,
        )

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None
    assert row.username is None
    assert row.login_type is LoginType.FEISHU
    assert row.result is LoginResultEnum.FAILURE
    assert row.reason == ErrorCode.FEISHU_UPSTREAM_ERROR.name


async def test_unexpected_login_exception_records_internal_error(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = unique_request_id()
    client = FakeFeishuClient(
        open_id="ou-unexpected",
        error=RuntimeError("unexpected upstream failure"),
    )

    with request_context(request_id), pytest.raises(RuntimeError):
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.16",
            client=client,
        )

    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.result is LoginResultEnum.FAILURE
    assert row.reason == "INTERNAL_ERROR"


async def test_list_login_logs_applies_filters(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-list")
    await add_local_user(
        session,
        username=username,
        password="password1",
    )
    request_id = unique_request_id()

    with request_context(request_id):
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password="password1",
            client_ip="203.0.113.17",
            device_id="device-list",
        )

    async with login_log_factory() as lookup_session:
        rows, total = await login_log_repository.list_login_logs(
            lookup_session,
            page=1,
            page_size=20,
            user_id=None,
            username=username,
            login_type=LoginType.LOCAL,
            result=LoginResultEnum.SUCCESS,
            ip="203.0.113.17",
            start_time=None,
            end_time=None,
        )

    matching_rows = [row for row in rows if row.request_id == request_id]
    assert total >= 1
    assert len(matching_rows) == 1
    assert matching_rows[0].device_id == "device-list"


async def test_list_login_logs_normalizes_aware_time_range(
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-aware-time")
    request_id = unique_request_id()
    created_at = datetime(2050, 1, 2, 0, 0, 0)

    async with login_log_factory() as write_session:
        write_session.add(
            LoginLog(
                user_id=None,
                username=username,
                login_type=LoginType.LOCAL,
                result=LoginResultEnum.SUCCESS,
                request_id=request_id,
                created_at=created_at,
            )
        )
        await write_session.commit()

    offset = timezone(timedelta(hours=8))
    start_time = datetime(2050, 1, 2, 8, 0, 0, tzinfo=offset)
    end_time = start_time + timedelta(seconds=1)

    async with login_log_factory() as lookup_session:
        rows, total = await login_log_repository.list_login_logs(
            lookup_session,
            page=1,
            page_size=20,
            user_id=None,
            username=username,
            login_type=None,
            result=None,
            ip=None,
            start_time=start_time,
            end_time=end_time,
        )

    assert total == 1
    assert [row.request_id for row in rows] == [request_id]


async def test_login_failure_without_request_meta_records_client_ip(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = unique_username("login-log-authoritative-ip")
    client_ip = "203.0.113.99"

    with pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password="wrong-password",
            client_ip=client_ip,
            device_id="device-authoritative-ip",
        )

    assert error.value.code == ErrorCode.INVALID_CREDENTIALS
    async with login_log_factory() as lookup_session:
        rows = list(
            (
                await lookup_session.scalars(
                    select(LoginLog).where(LoginLog.username == username)
                )
            ).all()
        )
        await lookup_session.execute(
            delete(LoginLog).where(LoginLog.username == username)
        )
        await lookup_session.commit()

    assert len(rows) == 1
    assert rows[0].ip == client_ip
    assert rows[0].user_agent is None
