import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis, key
from app.core.security import decode_access_token
from app.db.session import get_session
from app.main import create_app
from app.models.role import DataScope, Role, RoleStatus
from app.models.user import UserStatus
from app.schemas.user import UserCreate
from app.services import auth as auth_service
from app.services.user import create_local_user

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


@pytest.fixture(autouse=True)
def strong_test_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-" + "x" * 32)


class FakeRedis:
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

    async def incr(self, name: str) -> int:
        value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(value)
        return value

    async def expire(
        self,
        name: str,
        time: int,
        *,
        nx: bool = False,
    ) -> bool:
        if name not in self.values:
            return False
        if nx and name in self.expirations:
            return False
        self.expirations[name] = time
        return True

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

        if "redis.call('EXISTS'" in script:
            invalidation_key, snapshot_key = keys
            if self.values.get(invalidation_key) is not None:
                return 0
            self.values[snapshot_key] = values[0]
            self.expirations[snapshot_key] = int(values[1])
            return 1

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


@pytest_asyncio.fixture
async def real_redis() -> AsyncIterator[Redis]:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await redis.ping()
    try:
        yield redis
    finally:
        await redis.aclose()


@pytest_asyncio.fixture
async def live_client(
    session: AsyncSession,
    real_redis: Redis,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_redis] = lambda: real_redis

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        application.dependency_overrides.clear()


def unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def login(
    client: AsyncClient,
    username: str,
    password: str,
    *,
    device_id: str = "device-a",
) -> Response:
    return await client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": password,
            "device_id": device_id,
        },
    )


async def refresh(client: AsyncClient, refresh_token: str) -> Response:
    return await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )


async def test_local_login_returns_tokens_and_profile(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("login")
    password = "correct-password"
    user = await create_local_user(
        session,
        UserCreate(username=username, password=password),
    )

    response = await login(client, username, password)

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["profile"]["id"] == user.id
    assert data["profile"]["username"] == username

    await session.refresh(user)
    assert user.last_login_at is not None


async def test_local_login_with_wrong_password_returns_1001(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("wrong-password")
    await create_local_user(
        session,
        UserCreate(username=username, password="correct-password"),
    )

    response = await login(client, username, "wrong-password")

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.INVALID_CREDENTIALS


async def test_disabled_user_cannot_login(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("disabled")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    user.status = UserStatus.DISABLED
    await session.commit()

    response = await login(client, username, "password")

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.ACCOUNT_DISABLED


async def test_me_returns_effective_data_scope(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("me-scope")
    await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    login_response = await login(client, username, "password")
    access_token = login_response.json()["data"]["access_token"]

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    profile = response.json()["data"]
    assert profile["username"] == username
    assert profile["data_scope"] == DataScope.SELF
    assert profile["permission_codes"] == []


async def test_access_token_carries_token_version(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("claims")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )

    response = await login(client, username, "password")

    token = response.json()["data"]["access_token"]
    claims = decode_access_token(token)
    assert claims["sub"] == str(user.id)
    assert claims["ver"] == user.token_version
    assert claims["jti"]
    assert datetime.fromtimestamp(claims["exp"], tz=UTC) > datetime.now(UTC)


async def test_refresh_rotates_and_old_refresh_is_rejected(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("refresh")
    await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    login_response = await login(client, username, "password")
    old_refresh_token = login_response.json()["data"]["refresh_token"]

    rotated = await refresh(client, old_refresh_token)

    assert rotated.status_code == 200
    new_refresh_token = rotated.json()["data"]["refresh_token"]
    assert new_refresh_token != old_refresh_token

    reused = await refresh(client, old_refresh_token)

    assert reused.status_code == 401
    assert reused.json()["code"] == ErrorCode.TOKEN_INVALID

    next_rotation = await refresh(client, new_refresh_token)
    assert next_rotation.status_code == 200


async def test_logout_invalidates_refresh_token(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("logout")
    await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    login_response = await login(client, username, "password")
    refresh_token = login_response.json()["data"]["refresh_token"]

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    rejected = await refresh(client, refresh_token)
    assert rejected.status_code == 401
    assert rejected.json()["code"] == ErrorCode.TOKEN_INVALID


async def test_logout_all_removes_every_device(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("logout-all")
    await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    first = await login(client, username, "password", device_id="device-a")
    second = await login(client, username, "password", device_id="device-b")
    first_data = first.json()["data"]
    second_data = second.json()["data"]

    response = await client.post(
        "/api/v1/auth/logout-all",
        headers={"Authorization": f"Bearer {first_data['access_token']}"},
    )

    assert response.status_code == 200
    assert (await refresh(client, first_data["refresh_token"])).status_code == 401
    assert (await refresh(client, second_data["refresh_token"])).status_code == 401


async def test_access_token_rejected_after_token_version_bump(
    client: AsyncClient,
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    username = unique_username("version")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    login_response = await login(client, username, "password")
    access_token = login_response.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    await auth_service.bump_token_version(session, user.id, redis=fake_redis)

    response = await client.get("/api/v1/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.TOKEN_EXPIRED


async def test_auth_snapshot_cache_is_populated_and_invalidated(
    client: AsyncClient,
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    username = unique_username("snapshot")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    login_response = await login(client, username, "password")
    access_token = login_response.json()["data"]["access_token"]
    snapshot_key = key("auth", "snapshot", str(user.id))

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    raw_snapshot = await fake_redis.get(snapshot_key)
    assert raw_snapshot is not None
    snapshot = json.loads(raw_snapshot)
    assert snapshot == {
        "status": UserStatus.ACTIVE.value,
        "token_version": user.token_version,
        "role_id": None,
        "department_id": None,
    }
    assert fake_redis.expirations[snapshot_key] == 600

    await auth_service.invalidate_auth_snapshot(fake_redis, user.id)

    assert await fake_redis.get(snapshot_key) is None


@pytest.mark.parametrize("role_kind", ["missing", "disabled"])
async def test_disabled_or_missing_role_degrades_to_self_without_permissions(
    client: AsyncClient,
    session: AsyncSession,
    fake_redis: FakeRedis,
    role_kind: str,
) -> None:
    username = unique_username(f"degraded-{role_kind}")
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    if role_kind == "disabled":
        role = Role(
            code=unique_username("disabled-role"),
            name="Disabled role",
            data_scope=DataScope.ALL,
            data_scope_include_children=True,
            is_system=False,
            status=RoleStatus.DISABLED,
        )
        session.add(role)
        await session.commit()
        await session.refresh(role)
        user.role_id = role.id
        await session.commit()

    login_response = await login(client, username, "password")
    access_token = login_response.json()["data"]["access_token"]

    context = await get_current_context(
        session=session,
        redis=fake_redis,
        authorization=f"Bearer {access_token}",
    )

    assert context.role_id == user.role_id
    assert context.data_scope == DataScope.SELF
    assert context.permission_codes == frozenset()
    assert context.is_super_admin is False


async def test_login_rate_limit_returns_1005_after_threshold(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    username = unique_username("rate-limit")
    await create_local_user(
        session,
        UserCreate(username=username, password="correct-password"),
    )

    for _ in range(auth_service.LOGIN_RATE_LIMIT_ATTEMPTS):
        response = await login(client, username, "wrong-password")
        assert response.status_code == 401
        assert response.json()["code"] == ErrorCode.INVALID_CREDENTIALS

    response = await login(client, username, "wrong-password")

    assert response.status_code == 429
    assert response.json()["code"] == ErrorCode.RATE_LIMITED


async def test_account_rate_limit_normalizes_case_and_whitespace(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    username = unique_username("rate-case")
    await create_local_user(
        session,
        UserCreate(username=username, password="correct-password"),
    )
    variants = [
        username.upper(),
        username.lower(),
        username.title(),
        f" {username} ",
        f"\t{username.upper()} ",
    ]

    for index, variant in enumerate(variants):
        with pytest.raises(BusinessError) as error:
            await auth_service.login_local_user(
                session,
                fake_redis,
                username=variant,
                password="wrong-password",
                client_ip=f"198.51.100.{index + 1}",
                device_id=f"device-{index}",
            )
        assert error.value.code == ErrorCode.INVALID_CREDENTIALS

    with pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=f" {username.casefold()} ",
            password="wrong-password",
            client_ip="203.0.113.1",
            device_id="device-threshold",
        )

    assert error.value.code == ErrorCode.RATE_LIMITED
    assert error.value.http_status == 429


async def test_rate_limit_reservation_is_atomic_for_concurrent_attempts(
    fake_redis: FakeRedis,
) -> None:
    username = unique_username("rate-atomic")
    client_ip = "198.51.100.99"

    async def attempt() -> int | None:
        try:
            await auth_service.enforce_login_rate_limit(
                fake_redis,
                client_ip=client_ip,
                username=username,
            )
        except BusinessError as error:
            return error.code
        return None

    results = await asyncio.gather(*(attempt() for _ in range(10)))

    assert results.count(None) == auth_service.LOGIN_RATE_LIMIT_ATTEMPTS
    assert results.count(ErrorCode.RATE_LIMITED) == (
        10 - auth_service.LOGIN_RATE_LIMIT_ATTEMPTS
    )


async def test_rate_limit_reservation_uses_real_redis_lua(
    real_redis: Redis,
) -> None:
    username = unique_username("rate-live")
    client_ip = "203.0.113.250"
    ip_key = key("auth", "login", "ip", client_ip)
    account_key = key("auth", "login", "account", username.casefold())
    await real_redis.delete(ip_key, account_key)

    try:
        for _ in range(auth_service.LOGIN_RATE_LIMIT_ATTEMPTS):
            await auth_service.enforce_login_rate_limit(
                real_redis,
                client_ip=client_ip,
                username=username,
            )

        with pytest.raises(BusinessError) as error:
            await auth_service.enforce_login_rate_limit(
                real_redis,
                client_ip=client_ip,
                username=username,
            )

        assert error.value.code == ErrorCode.RATE_LIMITED
        assert await real_redis.get(account_key) == str(
            auth_service.LOGIN_RATE_LIMIT_ATTEMPTS + 1
        )
        assert (
            0
            < await real_redis.ttl(account_key)
            <= (auth_service.LOGIN_RATE_LIMIT_WINDOW_SECONDS)
        )
    finally:
        await real_redis.delete(ip_key, account_key)


async def test_rate_limit_increments_before_password_verification(
    session: AsyncSession,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = unique_username("rate-order")
    await create_local_user(
        session,
        UserCreate(username=username, password="correct-password"),
    )
    client_ip = "198.51.100.100"
    observed_counts: dict[str, str | None] = {}

    def observing_verify_password(plain: str, hashed: str) -> bool:
        assert plain == "wrong-password"
        assert hashed
        observed_counts["ip"] = fake_redis.values.get(
            key("auth", "login", "ip", client_ip)
        )
        observed_counts["account"] = fake_redis.values.get(
            key("auth", "login", "account", username.casefold())
        )
        return False

    monkeypatch.setattr(auth_service, "verify_password", observing_verify_password)

    with pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=username,
            password="wrong-password",
            client_ip=client_ip,
            device_id="device-rate-order",
        )

    assert error.value.code == ErrorCode.INVALID_CREDENTIALS
    assert observed_counts == {"ip": "1", "account": "1"}


async def test_successful_login_clears_only_account_rate_limit(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    username = unique_username("rate-clear")
    password = "correct-password"
    client_ip = "198.51.100.200"
    await create_local_user(
        session,
        UserCreate(username=username, password=password),
    )
    ip_key = key("auth", "login", "ip", client_ip)
    account_key = key("auth", "login", "account", username.casefold())

    with pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=f" {username.upper()} ",
            password="wrong-password",
            client_ip=client_ip,
            device_id="device-rate-clear",
        )
    assert error.value.code == ErrorCode.INVALID_CREDENTIALS

    result = await auth_service.login_local_user(
        session,
        fake_redis,
        username=username,
        password=password,
        client_ip=client_ip,
        device_id="device-rate-clear",
    )

    assert result.tokens.refresh_token
    assert await fake_redis.get(account_key) is None
    assert await fake_redis.get(ip_key) == "2"
    assert (
        fake_redis.expirations[ip_key] == auth_service.LOGIN_RATE_LIMIT_WINDOW_SECONDS
    )


async def test_missing_user_still_runs_dummy_password_verification(
    session: AsyncSession,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_hashes: list[str] = []

    def observing_verify_password(plain: str, hashed: str) -> bool:
        assert plain == "wrong-password"
        verified_hashes.append(hashed)
        return False

    monkeypatch.setattr(auth_service, "verify_password", observing_verify_password)

    with pytest.raises(BusinessError) as error:
        await auth_service.login_local_user(
            session,
            fake_redis,
            username=unique_username("missing-user"),
            password="wrong-password",
            client_ip="198.51.100.201",
            device_id="device-missing-user",
        )

    assert error.value.code == ErrorCode.INVALID_CREDENTIALS
    assert verified_hashes == [auth_service._DUMMY_PASSWORD_HASH]


async def test_refresh_rotation_uses_real_redis_lua_and_resets_ttl(
    live_client: AsyncClient,
    session: AsyncSession,
    real_redis: Redis,
) -> None:
    username = unique_username("live-refresh")
    device_id = f"live-{uuid4().hex}"
    user = await create_local_user(
        session,
        UserCreate(username=username, password="password"),
    )
    login_response = await login(
        live_client,
        username,
        "password",
        device_id=device_id,
    )
    old_refresh_token = login_response.json()["data"]["refresh_token"]
    refresh_key = key("auth", "refresh", str(user.id), device_id)
    rate_limit_keys = (
        key("auth", "login", "ip", "127.0.0.1"),
        key("auth", "login", "account", username.casefold()),
    )

    try:
        initial_ttl = await real_redis.ttl(refresh_key)
        assert 0 < initial_ttl <= auth_service.REFRESH_TOKEN_TTL_SECONDS

        rotated = await refresh(live_client, old_refresh_token)

        assert rotated.status_code == 200
        new_refresh_token = rotated.json()["data"]["refresh_token"]
        assert new_refresh_token != old_refresh_token
        rotated_ttl = await real_redis.ttl(refresh_key)
        assert 0 < rotated_ttl <= auth_service.REFRESH_TOKEN_TTL_SECONDS

        reused = await refresh(live_client, old_refresh_token)

        assert reused.status_code == 401
        assert reused.json()["code"] == ErrorCode.TOKEN_INVALID
    finally:
        await real_redis.delete(refresh_key, *rate_limit_keys)


async def test_malformed_access_token_returns_1004(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.TOKEN_INVALID
