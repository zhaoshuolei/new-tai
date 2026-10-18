import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.clients.feishu import (
    FEISHU_APP_ACCESS_TOKEN_URL,
    FEISHU_TOKEN_URL,
    FEISHU_USER_INFO_URL,
    FeishuClient,
    FeishuUserInfo,
)
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import get_redis
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.models.integration_config import IntegrationConfig
from app.models.role import DataScope, Role, RoleStatus
from app.models.user import User, UserStatus
from app.schemas.integration import FeishuConfigSource, FeishuIntegrationUpdate
from app.services import auth as auth_service
from app.services.feishu_integration import (
    EffectiveFeishuConfig,
    build_feishu_client,
    resolve_feishu_config,
    save_feishu_integration,
)
from tests.feishu_helpers import add_feishu_config

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


class FakeFeishuClient(FeishuClient):
    def __init__(
        self,
        *,
        open_id: str | None = None,
        name: str = "Feishu User",
        avatar_url: str | None = "https://example.com/avatar.png",
        email: str | None = "feishu@example.com",
    ) -> None:
        self.open_id = open_id or f"ou-{uuid4().hex}"
        self.name = name
        self.avatar_url = avatar_url
        self.email = email
        self.exchange_calls = 0
        self.info_calls = 0

    async def exchange_code(self, code: str) -> str:
        self.exchange_calls += 1
        assert code == "valid-code"
        return "user-access-token"

    async def get_user_info(self, user_access_token: str) -> FeishuUserInfo:
        self.info_calls += 1
        assert user_access_token == "user-access-token"
        return FeishuUserInfo(
            open_id=self.open_id,
            name=self.name,
            avatar_url=self.avatar_url,
            email=self.email,
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
async def feishu_defaults(
    session: AsyncSession,
) -> tuple[Department, Role]:
    pending = Department(
        name="待分配",
        path="/1/",
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    default_role = Role(
        code="default",
        name="默认角色",
        data_scope=DataScope.SELF,
        data_scope_include_children=False,
        is_system=True,
        status=RoleStatus.ACTIVE,
    )
    session.add_all([pending, default_role])
    await session.commit()
    await session.refresh(pending)
    await session.refresh(default_role)
    return pending, default_role


@pytest_asyncio.fixture
async def configured_feishu(session: AsyncSession) -> IntegrationConfig:
    return await add_feishu_config(session)


@pytest.fixture(autouse=True)
def strong_test_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-" + "x" * 32)


@pytest_asyncio.fixture
async def client(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    application = create_app()
    feishu_client = FakeFeishuClient(open_id="ou-api-login")
    monkeypatch.setattr(
        auth_service,
        "build_feishu_client",
        lambda _config: feishu_client,
    )

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


async def _add_existing_user(
    session: AsyncSession,
    *,
    open_id: str,
    nickname: str | None = None,
    email: str | None = None,
    avatar: str | None = None,
    last_login_at: datetime | None = None,
) -> User:
    user = User(
        feishu_open_id=open_id,
        nickname=nickname,
        email=email,
        avatar=avatar,
        status=UserStatus.ACTIVE,
        last_login_at=last_login_at,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_first_login_auto_provisions_user(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
) -> None:
    pending, default_role = feishu_defaults
    feishu_client = FakeFeishuClient(open_id="ou-first-login")

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=feishu_client,
    )
    user = result.user

    assert result.tokens.access_token
    assert result.tokens.refresh_token
    assert user.id is not None
    assert user.feishu_open_id == "ou-first-login"
    assert user.department_id == pending.id
    assert user.role_id == default_role.id
    assert user.last_login_at is not None


async def test_new_user_lands_in_pending_department_with_default_role(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
) -> None:
    pending, default_role = feishu_defaults

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(open_id="ou-provisioned"),
    )
    user = result.user

    assert user.department_id == pending.id
    assert user.role_id == default_role.id
    assert user.status == UserStatus.ACTIVE
    assert user.username is None
    assert user.password_hash is None


async def test_existing_user_reuses_record_by_open_id(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    existing = await _add_existing_user(session, open_id="ou-existing")

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(open_id="ou-existing"),
    )
    user = result.user

    count = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.feishu_open_id == "ou-existing")
    )
    assert user.id == existing.id
    assert count == 1


async def test_login_backfills_only_empty_profile_fields(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    user = await _add_existing_user(session, open_id="ou-backfill")

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(
            open_id="ou-backfill",
            name="Filled Nickname",
            email="filled@example.com",
        ),
    )
    logged_in = result.user

    assert logged_in.id == user.id
    assert logged_in.nickname == "Filled Nickname"
    assert logged_in.email == "filled@example.com"


async def test_admin_edited_nickname_is_not_overwritten(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    user = await _add_existing_user(
        session,
        open_id="ou-admin-edited",
        nickname="Admin Edited",
        email="admin-edited@example.com",
    )

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(
            open_id="ou-admin-edited",
            name="Feishu Name",
            email="feishu-email@example.com",
        ),
    )
    logged_in = result.user

    assert logged_in.id == user.id
    assert logged_in.nickname == "Admin Edited"
    assert logged_in.email == "admin-edited@example.com"


async def test_avatar_is_only_backfilled_when_empty(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    previous_login = datetime(2026, 1, 1)
    user = await _add_existing_user(
        session,
        open_id="ou-refresh",
        avatar="https://example.com/old.png",
        last_login_at=previous_login,
    )

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(
            open_id="ou-refresh",
            avatar_url="https://example.com/new.png",
        ),
    )
    logged_in = result.user

    assert logged_in.id == user.id
    assert logged_in.avatar == "https://example.com/old.png"
    assert logged_in.last_login_at is not None
    assert logged_in.last_login_at > previous_login
    assert logged_in.last_login_at.tzinfo is None
    assert logged_in.last_login_at <= datetime.now(UTC).replace(tzinfo=None)


async def test_avatar_is_backfilled_when_locally_empty(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    user = await _add_existing_user(
        session,
        open_id="ou-avatar-backfill",
        avatar=None,
    )

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(
            open_id="ou-avatar-backfill",
            avatar_url="https://example.com/first.png",
        ),
    )
    logged_in = result.user

    assert logged_in.id == user.id
    assert logged_in.avatar == "https://example.com/first.png"


@pytest.mark.parametrize("empty_avatar", [None, ""])
async def test_empty_feishu_avatar_does_not_clear_stored_avatar(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    empty_avatar: str | None,
) -> None:
    previous_login = datetime(2026, 1, 1)
    user = await _add_existing_user(
        session,
        open_id="ou-avatar-preserved",
        avatar="https://example.com/admin-avatar.png",
        last_login_at=previous_login,
    )

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(
            open_id="ou-avatar-preserved",
            avatar_url=empty_avatar,
        ),
    )
    logged_in = result.user

    assert logged_in.id == user.id
    assert logged_in.avatar == "https://example.com/admin-avatar.png"
    assert logged_in.last_login_at is not None
    assert logged_in.last_login_at > previous_login


async def test_pending_department_lookup_is_identity_based(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    other_system_department = Department(
        name="其他系统节点",
        path="/1/",
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    session.add(other_system_department)
    await session.commit()
    await session.refresh(other_system_department)

    pending = Department(
        name="待分配",
        path="/2/",
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    default_role = Role(
        code="default",
        name="默认角色",
        data_scope=DataScope.SELF,
        data_scope_include_children=False,
        is_system=True,
        status=RoleStatus.ACTIVE,
    )
    session.add_all([pending, default_role])
    await session.commit()
    await session.refresh(pending)
    await session.refresh(default_role)

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(open_id="ou-pending-identity"),
    )
    user = result.user

    assert user.department_id == pending.id
    assert user.department_id != other_system_department.id


async def test_duplicate_open_id_race_recovers_existing_user(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_id = f"ou-race-{uuid4().hex}"
    winner = await _add_existing_user(
        session,
        open_id=open_id,
        nickname="Race Winner",
    )
    original_lookup = auth_service._get_feishu_user_by_open_id
    lookup_calls = 0

    async def racing_lookup(
        lookup_session: AsyncSession,
        lookup_open_id: str,
    ) -> User | None:
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return await original_lookup(lookup_session, lookup_open_id)

    monkeypatch.setattr(auth_service, "_get_feishu_user_by_open_id", racing_lookup)

    result = await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.10",
        client=FakeFeishuClient(open_id=open_id),
    )
    logged_in = result.user

    assert lookup_calls == 2
    assert logged_in.id == winner.id
    assert logged_in.nickname == "Race Winner"


async def test_duplicate_open_id_race_without_winner_returns_business_error(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_id = f"ou-race-missing-{uuid4().hex}"

    async def missing_lookup(
        _lookup_session: AsyncSession,
        _lookup_open_id: str,
    ) -> User | None:
        return None

    monkeypatch.setattr(auth_service, "_get_feishu_user_by_open_id", missing_lookup)

    original_commit = session.commit
    failed_user_commit = False

    async def racing_commit() -> None:
        nonlocal failed_user_commit
        if not failed_user_commit and any(
            isinstance(item, User) for item in session.new
        ):
            failed_user_commit = True
            raise IntegrityError(
                "INSERT user",
                {},
                Exception("duplicate feishu_open_id"),
            )
        await original_commit()

    monkeypatch.setattr(session, "commit", racing_commit)

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.10",
            client=FakeFeishuClient(open_id=open_id),
        )

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert error.value.http_status == 502


async def test_disabled_user_login_returns_1002(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
) -> None:
    user = await _add_existing_user(session, open_id="ou-disabled")
    user.status = UserStatus.DISABLED
    await session.commit()

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.10",
            client=FakeFeishuClient(open_id="ou-disabled"),
        )

    assert error.value.code == ErrorCode.ACCOUNT_DISABLED
    assert error.value.http_status == 401


async def test_feishu_error_response_maps_to_business_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": 20003,
                "error": "invalid_grant",
                "error_description": "authorization code not found",
            },
        )

    feishu_client = FeishuClient(
        transport=httpx.MockTransport(handler),
        app_id="cli-test",
        app_secret="secret",
    )

    with pytest.raises(BusinessError) as error:
        await feishu_client.exchange_code("invalid-code")

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert error.value.http_status == 502


async def test_transport_error_maps_to_business_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    feishu_client = FeishuClient(transport=httpx.MockTransport(handler))

    with pytest.raises(BusinessError) as error:
        await feishu_client.exchange_code("valid-code")

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert error.value.http_status == 502


async def test_exchange_code_uses_v3_endpoint_and_form_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "access_token": "user-access-token"},
        )

    feishu_client = FeishuClient(
        transport=httpx.MockTransport(handler),
        app_id="cli-test",
        app_secret="secret",
        redirect_uri="https://example.com/callback",
    )

    token = await feishu_client.exchange_code("authorization-code")

    assert token == "user-access-token"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == FEISHU_TOKEN_URL
    assert request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert dict(parse_qsl(request.content.decode())) == {
        "grant_type": "authorization_code",
        "client_id": "cli-test",
        "client_secret": "secret",
        "code": "authorization-code",
        "redirect_uri": "https://example.com/callback",
    }


async def test_database_empty_redirect_does_not_use_environment_redirect(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session, redirect_uri="")
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    monkeypatch.setattr(
        settings,
        "feishu_redirect_uri",
        "https://environment.example.com/callback",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "access_token": "user-access-token"},
        )

    config = await resolve_feishu_config(session)
    feishu_client = build_feishu_client(config)
    feishu_client._transport = httpx.MockTransport(handler)

    await feishu_client.exchange_code("authorization-code")

    assert len(requests) == 1
    body = dict(parse_qsl(requests[0].content.decode()))
    assert body["client_id"] == "cli-test"
    assert "redirect_uri" not in body


async def test_verify_app_credentials_posts_json_and_discards_tokens() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "app_access_token": "must-not-be-returned",
                "tenant_access_token": "must-not-be-returned",
                "expire": 7200,
            },
        )

    client = FeishuClient(
        transport=httpx.MockTransport(handler),
        app_id="cli-test",
        app_secret="secret-value",
    )

    assert await client.verify_app_credentials() is None
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == FEISHU_APP_ACCESS_TOKEN_URL
    assert requests[0].headers["content-type"].startswith("application/json")
    assert json.loads(requests[0].content) == {
        "app_id": "cli-test",
        "app_secret": "secret-value",
    }


async def test_verify_app_credentials_maps_rejection_to_8011() -> None:
    client = FeishuClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"code": 10003, "msg": "no"})
        ),
        app_id="cli-bad",
        app_secret="bad-secret",
    )

    with pytest.raises(BusinessError) as error:
        await client.verify_app_credentials()

    assert error.value.code == ErrorCode.FEISHU_CREDENTIALS_REJECTED
    assert error.value.http_status == 422


async def test_verify_app_credentials_maps_bad_response_to_1011() -> None:
    client = FeishuClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"msg": "missing-code"})
        ),
        app_id="cli-test",
        app_secret="secret-value",
    )

    with pytest.raises(BusinessError) as error:
        await client.verify_app_credentials()

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert error.value.http_status == 502


@pytest.mark.parametrize("status_code", [400, 500])
@pytest.mark.parametrize("code", [0, 10003])
async def test_verify_app_credentials_maps_non_success_http_to_1011(
    status_code: int,
    code: int,
) -> None:
    client = FeishuClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json={"code": code})
        ),
        app_id="cli-test",
        app_secret="secret-value",
    )

    with pytest.raises(BusinessError) as error:
        await client.verify_app_credentials()

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert error.value.http_status == 502


async def test_get_user_info_uses_bearer_token_and_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == FEISHU_USER_INFO_URL
        assert request.headers["authorization"] == "Bearer user-access-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "open_id": "ou-test",
                    "name": "Test User",
                    "avatar_url": "https://example.com/avatar.png",
                    "email": "test@example.com",
                },
            },
        )

    feishu_client = FeishuClient(transport=httpx.MockTransport(handler))

    info = await feishu_client.get_user_info("user-access-token")

    assert info == FeishuUserInfo(
        open_id="ou-test",
        name="Test User",
        avatar_url="https://example.com/avatar.png",
        email="test@example.com",
    )


async def test_feishu_login_rate_limit_precedes_upstream_exchange(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app()
    feishu_client = FakeFeishuClient(open_id="ou-rate-limited")
    monkeypatch.setattr(
        auth_service,
        "build_feishu_client",
        lambda _config: feishu_client,
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as test_client:
            for _ in range(auth_service.LOGIN_RATE_LIMIT_ATTEMPTS):
                response = await test_client.post(
                    "/api/v1/auth/feishu/login",
                    json={"code": "valid-code"},
                )
                assert response.status_code == 200

            blocked = await test_client.post(
                "/api/v1/auth/feishu/login",
                json={"code": "valid-code"},
            )
    finally:
        application.dependency_overrides.clear()

    assert blocked.status_code == 429
    assert blocked.json()["code"] == ErrorCode.RATE_LIMITED
    assert feishu_client.exchange_calls == auth_service.LOGIN_RATE_LIMIT_ATTEMPTS
    assert feishu_client.info_calls == auth_service.LOGIN_RATE_LIMIT_ATTEMPTS


async def test_login_resolves_database_config_after_rate_limit(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    fake = FakeFeishuClient(open_id="ou-db-config")
    seen: list[EffectiveFeishuConfig] = []

    def build(config: EffectiveFeishuConfig) -> FeishuClient:
        seen.append(config)
        return fake

    monkeypatch.setattr(auth_service, "build_feishu_client", build)

    await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.30",
    )

    assert len(seen) == 1
    assert seen[0].source is FeishuConfigSource.DATABASE
    assert seen[0].app_id == "cli-test"
    assert seen[0].app_secret == "secret-value"


async def test_login_commits_config_read_before_exchange(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeFeishuClient(open_id="ou-committed-config")
    transaction_states: list[bool] = []

    async def exchange_code(_code: str) -> str:
        transaction_states.append(session.in_transaction())
        return "user-access-token"

    monkeypatch.setattr(client, "exchange_code", exchange_code)

    await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.36",
        client=client,
    )

    assert transaction_states == [False]


async def test_login_without_config_returns_8009_before_upstream(
    session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    client = FakeFeishuClient(open_id="ou-no-config")

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.31",
            client=client,
        )

    assert error.value.code == ErrorCode.FEISHU_NOT_CONFIGURED
    assert client.exchange_calls == 0


async def test_login_with_damaged_database_config_returns_8010(
    session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    client = FakeFeishuClient(open_id="ou-damaged-config")

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.32",
            client=client,
        )

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert client.exchange_calls == 0


async def test_rate_limit_precedes_config_resolution(
    session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_ip = "203.0.113.34"
    for _ in range(auth_service.LOGIN_RATE_LIMIT_ATTEMPTS):
        await auth_service.enforce_login_ip_rate_limit(fake_redis, client_ip=client_ip)
    resolve = AsyncMock(side_effect=AssertionError("resolver must not run"))
    monkeypatch.setattr(auth_service, "resolve_feishu_config", resolve)

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip=client_ip,
        )

    assert error.value.code == ErrorCode.RATE_LIMITED
    assert resolve.await_count == 0


async def test_saved_config_is_used_by_next_login_without_restart(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFeishuClient(open_id="ou-after-save")
    seen: list[EffectiveFeishuConfig] = []
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        auth_service,
        "build_feishu_client",
        lambda config: seen.append(config) or fake,
    )
    context = AuthContext(
        user_id=7,
        role_id=None,
        department_id=None,
        data_scope=None,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset({"integration_feishu:update"}),
        is_super_admin=False,
    )

    await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(app_id="cli-new", app_secret="new-secret"),
        context=context,
    )
    await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.33",
    )

    assert seen[-1].app_id == "cli-new"
    assert seen[-1].app_secret == "new-secret"


async def test_login_endpoint_returns_local_token_shape(
    client: AsyncClient,
    feishu_defaults: tuple[Department, Role],
) -> None:
    response = await client.post(
        "/api/v1/auth/feishu/login",
        json={"code": "valid-code"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    assert data["profile"]["feishu_open_id"] == "ou-api-login"


async def test_login_endpoint_uses_issued_token_metadata(
    client: AsyncClient,
    feishu_defaults: tuple[Department, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def custom_issue_token_pair(
        _redis: Any,
        _user: User,
        *,
        device_id: str | None = None,
    ) -> auth_service.TokenPair:
        assert device_id is None
        return auth_service.TokenPair(
            access_token="issued-access-token",
            refresh_token="issued-refresh-token",
            token_type="custom",
            expires_in=123,
        )

    monkeypatch.setattr(auth_service, "issue_token_pair", custom_issue_token_pair)

    response = await client.post(
        "/api/v1/auth/feishu/login",
        json={"code": "valid-code"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["access_token"] == "issued-access-token"
    assert data["refresh_token"] == "issued-refresh-token"
    assert data["token_type"] == "custom"
    assert data["expires_in"] == 123
