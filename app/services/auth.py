"""Local authentication, token lifecycle and request auth snapshots."""

import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, cast
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import key
from app.core.redis import redis_client as default_redis_client
from app.core.security import create_access_token, verify_password
from app.models.role import DataScope, RoleDepartment, RoleStatus
from app.models.user import User, UserStatus
from app.repositories import role as role_repository
from app.repositories import user as user_repository
from app.services import role as role_service

AUTH_SNAPSHOT_TTL_SECONDS = 600
REFRESH_TOKEN_TTL_SECONDS = settings.refresh_token_expire_days * 24 * 60 * 60
LOGIN_RATE_LIMIT_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300

_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROTATE_REFRESH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""
_DELETE_IF_MATCH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
    return 0
end
return redis.call('DEL', KEYS[1])
"""


@dataclass(frozen=True)
class AuthSnapshot:
    status: UserStatus
    token_version: int
    role_id: int | None
    department_id: int | None

    def to_cache(self) -> str:
        return json.dumps(
            {
                "status": self.status.value,
                "token_version": self.token_version,
                "role_id": self.role_id,
                "department_id": self.department_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_cache(cls, raw: str) -> AuthSnapshot | None:
        try:
            payload: Any = json.loads(raw)
            status = UserStatus(payload["status"])
            token_version = payload["token_version"]
            role_id = payload["role_id"]
            department_id = payload["department_id"]
        except JSONDecodeError, KeyError, TypeError, ValueError:
            return None

        if (
            not isinstance(token_version, int)
            or isinstance(token_version, bool)
            or not _optional_int(role_id)
            or not _optional_int(department_id)
        ):
            return None
        return cls(
            status=status,
            token_version=token_version,
            role_id=role_id,
            department_id=department_id,
        )


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = settings.access_token_expire_minutes * 60


@dataclass(frozen=True)
class LoginResult:
    tokens: TokenPair
    user: User


def _optional_int(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _snapshot_key(user_id: int) -> str:
    return key("auth", "snapshot", str(user_id))


def _refresh_key(user_id: int, device_id: str) -> str:
    return key("auth", "refresh", str(user_id), device_id)


def _refresh_key_pattern(user_id: int) -> str:
    return key("auth", "refresh", str(user_id), "*")


def _login_ip_key(client_ip: str) -> str:
    return key("auth", "login", "ip", client_ip)


def _login_account_key(username: str) -> str:
    return key("auth", "login", "account", username)


async def get_auth_snapshot(
    session: AsyncSession,
    redis: Redis,
    user_id: int,
) -> AuthSnapshot:
    """Return the cached per-user auth snapshot, loading it on a miss."""
    snapshot_key = _snapshot_key(user_id)
    cached = await redis.get(snapshot_key)
    if isinstance(cached, str):
        snapshot = AuthSnapshot.from_cache(cached)
        if snapshot is not None:
            return snapshot
        await redis.delete(snapshot_key)

    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )

    snapshot = AuthSnapshot(
        status=user.status,
        token_version=user.token_version,
        role_id=user.role_id,
        department_id=user.department_id,
    )
    await redis.set(
        snapshot_key,
        snapshot.to_cache(),
        ex=AUTH_SNAPSHOT_TTL_SECONDS,
    )
    return snapshot


async def invalidate_auth_snapshot(redis: Redis, user_id: int) -> None:
    await redis.delete(_snapshot_key(user_id))


async def invalidate_refresh_tokens(redis: Redis, user_id: int) -> None:
    pattern = _refresh_key_pattern(user_id)
    async for refresh_key in redis.scan_iter(match=pattern):
        await redis.delete(refresh_key)


async def invalidate_user_sessions(
    redis: Redis,
    user_id: int,
) -> None:
    await invalidate_auth_snapshot(redis, user_id)
    await invalidate_refresh_tokens(redis, user_id)


async def bump_token_version(
    session: AsyncSession,
    user_id: int,
    *,
    redis: Redis | None = None,
) -> None:
    statement = (
        update(User)
        .where(User.id == user_id)
        .values(token_version=User.token_version + 1)
    )
    result = cast(CursorResult[Any], await session.execute(statement))
    if result.rowcount != 1:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )
    await session.commit()
    await invalidate_user_sessions(redis or default_redis_client, user_id)


async def authenticate_local_user(
    session: AsyncSession,
    username: str,
    password: str,
) -> User:
    user = await user_repository.get_by_username(session, username)
    if (
        user is None
        or user.password_hash is None
        or not verify_password(password, user.password_hash)
    ):
        raise BusinessError(
            ErrorCode.INVALID_CREDENTIALS,
            "Invalid username or password",
            http_status=401,
        )
    if user.status == UserStatus.DISABLED:
        raise BusinessError(
            ErrorCode.ACCOUNT_DISABLED,
            "Account disabled",
            http_status=401,
        )

    user.last_login_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    await session.refresh(user)
    return user


def _normalize_device_id(device_id: str | None) -> str:
    normalized = device_id or uuid4().hex
    if _DEVICE_ID_PATTERN.fullmatch(normalized) is None:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Invalid device id",
        )
    return normalized


def _make_refresh_token(
    user_id: int,
    device_id: str,
    token_version: int,
) -> str:
    secret = secrets.token_urlsafe(48)
    return f"{user_id}.{device_id}.{token_version}.{secret}"


def _parse_refresh_token(refresh_token: str) -> tuple[int, str, int, str]:
    try:
        user_id_text, device_id, token_version_text, secret = refresh_token.split(
            ".",
            3,
        )
        user_id = int(user_id_text)
        token_version = int(token_version_text)
    except TypeError, ValueError:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        ) from None

    if (
        user_id <= 0
        or token_version < 0
        or not secret
        or _DEVICE_ID_PATTERN.fullmatch(device_id) is None
    ):
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )
    return user_id, device_id, token_version, secret


async def issue_token_pair(
    redis: Redis,
    user: User,
    *,
    device_id: str | None = None,
) -> TokenPair:
    normalized_device_id = _normalize_device_id(device_id)
    refresh_token = _make_refresh_token(
        user.id,
        normalized_device_id,
        user.token_version,
    )
    await redis.set(
        _refresh_key(user.id, normalized_device_id),
        refresh_token,
        ex=REFRESH_TOKEN_TTL_SECONDS,
    )
    return TokenPair(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=refresh_token,
    )


async def login_local_user(
    session: AsyncSession,
    redis: Redis,
    *,
    username: str,
    password: str,
    client_ip: str,
    device_id: str | None = None,
) -> LoginResult:
    await enforce_login_rate_limit(
        redis,
        client_ip=client_ip,
        username=username,
    )
    try:
        user = await authenticate_local_user(session, username, password)
    except BusinessError as error:
        if error.code == ErrorCode.INVALID_CREDENTIALS:
            await record_login_failure(
                redis,
                client_ip=client_ip,
                username=username,
            )
        raise

    await clear_login_failures(
        redis,
        client_ip=client_ip,
        username=username,
    )
    tokens = await issue_token_pair(redis, user, device_id=device_id)
    return LoginResult(tokens=tokens, user=user)


async def rotate_refresh_token(
    session: AsyncSession,
    redis: Redis,
    refresh_token: str,
) -> TokenPair:
    user_id, device_id, token_version, _secret = _parse_refresh_token(refresh_token)
    user = await user_repository.get_by_id(session, user_id)
    if user is None or user.token_version != token_version:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )
    if user.status == UserStatus.DISABLED:
        raise BusinessError(
            ErrorCode.ACCOUNT_DISABLED,
            "Account disabled",
            http_status=401,
        )

    new_refresh_token = _make_refresh_token(
        user.id,
        device_id,
        user.token_version,
    )
    rotated = await redis.eval(
        _ROTATE_REFRESH_SCRIPT,
        1,
        _refresh_key(user.id, device_id),
        refresh_token,
        new_refresh_token,
        str(REFRESH_TOKEN_TTL_SECONDS),
    )
    if rotated != 1:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )
    return TokenPair(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=new_refresh_token,
    )


async def logout_refresh_token(redis: Redis, refresh_token: str) -> None:
    try:
        user_id, device_id, _token_version, _secret = _parse_refresh_token(
            refresh_token
        )
    except BusinessError:
        return

    await redis.eval(
        _DELETE_IF_MATCH_SCRIPT,
        1,
        _refresh_key(user_id, device_id),
        refresh_token,
    )


async def enforce_login_rate_limit(
    redis: Redis,
    *,
    client_ip: str,
    username: str,
) -> None:
    for rate_limit_key in (
        _login_ip_key(client_ip),
        _login_account_key(username),
    ):
        raw_count = await redis.get(rate_limit_key)
        try:
            count = int(raw_count or 0)
        except ValueError:
            count = 0
        if count >= LOGIN_RATE_LIMIT_ATTEMPTS:
            raise BusinessError(
                ErrorCode.RATE_LIMITED,
                "Too many login attempts",
                http_status=429,
            )


async def record_login_failure(
    redis: Redis,
    *,
    client_ip: str,
    username: str,
) -> None:
    for rate_limit_key in (
        _login_ip_key(client_ip),
        _login_account_key(username),
    ):
        count = await redis.incr(rate_limit_key)
        if count == 1:
            await redis.expire(
                rate_limit_key,
                LOGIN_RATE_LIMIT_WINDOW_SECONDS,
            )


async def clear_login_failures(
    redis: Redis,
    *,
    client_ip: str,
    username: str,
) -> None:
    await redis.delete(
        _login_ip_key(client_ip),
        _login_account_key(username),
    )


async def build_auth_context(
    session: AsyncSession,
    user_id: int,
    snapshot: AuthSnapshot,
) -> AuthContext:
    role = None
    if snapshot.role_id is not None:
        role = await role_repository.get_by_id(session, snapshot.role_id)

    if role is None or role.status == RoleStatus.DISABLED:
        return AuthContext(
            user_id=user_id,
            role_id=snapshot.role_id,
            department_id=snapshot.department_id,
            data_scope=DataScope.SELF,
            custom_department_ids=frozenset(),
            include_children=False,
            permission_codes=frozenset(),
            is_super_admin=False,
        )

    custom_department_ids: frozenset[int] = frozenset()
    if role.data_scope == DataScope.CUSTOM:
        result = await session.scalars(
            select(RoleDepartment.c.department_id).where(
                RoleDepartment.c.role_id == role.id
            )
        )
        custom_department_ids = frozenset(result.all())

    return AuthContext(
        user_id=user_id,
        role_id=role.id,
        department_id=snapshot.department_id,
        data_scope=role.data_scope,
        custom_department_ids=custom_department_ids,
        include_children=role.data_scope_include_children,
        permission_codes=await role_service.list_role_permission_codes(session, role),
        is_super_admin=role.code == "super_admin" and role.is_system,
    )


__all__: tuple[str, ...] = (
    "AUTH_SNAPSHOT_TTL_SECONDS",
    "LOGIN_RATE_LIMIT_ATTEMPTS",
    "LOGIN_RATE_LIMIT_WINDOW_SECONDS",
    "REFRESH_TOKEN_TTL_SECONDS",
    "AuthSnapshot",
    "LoginResult",
    "TokenPair",
    "authenticate_local_user",
    "build_auth_context",
    "bump_token_version",
    "clear_login_failures",
    "enforce_login_rate_limit",
    "get_auth_snapshot",
    "invalidate_auth_snapshot",
    "invalidate_refresh_tokens",
    "invalidate_user_sessions",
    "issue_token_pair",
    "login_local_user",
    "logout_refresh_token",
    "record_login_failure",
    "rotate_refresh_token",
)
