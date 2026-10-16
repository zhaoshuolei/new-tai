"""本地认证、令牌生命周期与请求认证快照。"""

import json
import logging
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
from app.core.permissions import get_role_permission_codes
from app.core.redis import key
from app.core.redis import redis_client as default_redis_client
from app.core.security import create_access_token, verify_password
from app.models.role import DataScope, RoleDepartment, RoleStatus
from app.models.user import User, UserStatus
from app.repositories import role as role_repository
from app.repositories import user as user_repository

AUTH_SNAPSHOT_TTL_SECONDS = 600
SESSION_INVALIDATION_FENCE_TTL_SECONDS = AUTH_SNAPSHOT_TTL_SECONDS * 2
REFRESH_TOKEN_TTL_SECONDS = settings.refresh_token_expire_days * 24 * 60 * 60
LOGIN_RATE_LIMIT_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300

logger = logging.getLogger(__name__)

_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$0sQwzdi5vx9ycTTSD1oUBA$"
    "UMXfsnrib5tjjU2VjKDaHA1v1ltjclIuw/27FiUvE9k"
)
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LOGIN_RATE_LIMIT_SCRIPT = """
local exceeded = 0
for _, rate_key in ipairs(KEYS) do
    local count = redis.call('INCR', rate_key)
    if count == 1 then
        redis.call('EXPIRE', rate_key, ARGV[1])
    end
    if count > tonumber(ARGV[2]) then
        exceeded = 1
    end
end
return exceeded
"""
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
_WRITE_SNAPSHOT_IF_NOT_INVALIDATING_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
return 1
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


def _session_invalidation_key(user_id: int) -> str:
    return key("auth", "invalidating", str(user_id))


def _refresh_key(user_id: int, device_id: str) -> str:
    return key("auth", "refresh", str(user_id), device_id)


def _refresh_key_pattern(user_id: int) -> str:
    return key("auth", "refresh", str(user_id), "*")


def _normalize_account(username: str) -> str:
    return username.strip().casefold()


def _login_ip_key(client_ip: str) -> str:
    return key("auth", "login", "ip", client_ip)


def _login_account_key(username: str) -> str:
    return key("auth", "login", "account", _normalize_account(username))


async def get_auth_snapshot(
    session: AsyncSession,
    redis: Redis,
    user_id: int,
) -> AuthSnapshot:
    """返回用户认证快照；缓存未命中时回源数据库并写回。"""
    snapshot_key = _snapshot_key(user_id)
    invalidation_key = _session_invalidation_key(user_id)
    invalidating = await redis.get(invalidation_key) is not None
    if invalidating:
        # 变更事务尚未提交，禁止使用或回填旧快照。
        await redis.delete(snapshot_key)
        cached = None
    else:
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
    if invalidating:
        return snapshot

    written = await redis.eval(
        _WRITE_SNAPSHOT_IF_NOT_INVALIDATING_SCRIPT,
        2,
        invalidation_key,
        snapshot_key,
        snapshot.to_cache(),
        str(AUTH_SNAPSHOT_TTL_SECONDS),
    )
    if written != 1:
        return snapshot
    return snapshot


async def invalidate_auth_snapshot(redis: Redis, user_id: int) -> None:
    await redis.delete(_snapshot_key(user_id))


async def _start_user_session_invalidation(redis: Redis, user_id: int) -> None:
    """在删除快照前立栅栏，阻止并发请求回填旧快照。"""
    await redis.set(
        _session_invalidation_key(user_id),
        "1",
        ex=SESSION_INVALIDATION_FENCE_TTL_SECONDS,
    )


async def _finish_user_session_invalidation(redis: Redis, user_id: int) -> None:
    """提交后再删一次，清理提交窗口内由旧行回填的快照。"""
    try:
        await invalidate_user_sessions(redis, user_id)
    except Exception:
        # 保留栅栏；其 TTL 长于快照 TTL，旧快照不会在栅栏过期后重生。
        logger.exception(
            "Post-commit session invalidation failed",
            extra={"user_id": user_id},
        )
        return
    try:
        await redis.delete(_session_invalidation_key(user_id))
    except Exception:
        # 栅栏残留只会让认证缓存暂时旁路，不会放行旧授权。
        logger.exception(
            "Session invalidation fence cleanup failed",
            extra={"user_id": user_id},
        )


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


async def _increment_token_version_in_transaction(
    session: AsyncSession,
    user_id: int,
) -> None:
    statement = (
        update(User)
        .where(User.id == user_id)
        .values(token_version=User.token_version + 1)
    )
    result = cast(CursorResult[Any], await session.execute(statement))
    if result.rowcount != 1:
        await session.rollback()
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )


async def commit_security_change(
    session: AsyncSession,
    user_id: int,
    *,
    redis: Redis,
    increment_token_version: bool = True,
) -> None:
    """提交安全变更并保证提交前后均完成会话失效。

    顺序固定为：事务内改字段/版本、设置失效栅栏、第一次删除、提交、
    第二次删除。第一次删除失败会回滚数据库；第二次删除失败保留栅栏，
    并发请求不会写入旧快照，且栅栏 TTL 长于快照 TTL。
    """
    if increment_token_version:
        await _increment_token_version_in_transaction(session, user_id)

    try:
        await _start_user_session_invalidation(redis, user_id)
        await invalidate_user_sessions(redis, user_id)
    except Exception as error:
        await session.rollback()
        raise BusinessError(
            ErrorCode.INTERNAL_ERROR,
            "Session invalidation failed; database change rolled back",
            http_status=503,
        ) from error

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    await _finish_user_session_invalidation(redis, user_id)


async def bump_token_version(
    session: AsyncSession,
    user_id: int,
    *,
    redis: Redis | None = None,
) -> None:
    await commit_security_change(
        session,
        user_id,
        redis=redis or default_redis_client,
    )


async def authenticate_local_user(
    session: AsyncSession,
    username: str,
    password: str,
) -> User:
    normalized_username = _normalize_account(username)
    user = await user_repository.get_by_username(session, normalized_username)
    password_hash = (
        user.password_hash
        if user is not None and user.password_hash is not None
        else _DUMMY_PASSWORD_HASH
    )
    password_matches = verify_password(password, password_hash)
    if user is None or user.password_hash is None or not password_matches:
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
    user = await authenticate_local_user(session, username, password)
    await clear_login_account_failures(redis, username=username)
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
    """原子预占 IP 与账号两个限流桶，超过阈值时拒绝登录。"""
    exceeded = await redis.eval(
        _LOGIN_RATE_LIMIT_SCRIPT,
        2,
        _login_ip_key(client_ip),
        _login_account_key(username),
        str(LOGIN_RATE_LIMIT_WINDOW_SECONDS),
        str(LOGIN_RATE_LIMIT_ATTEMPTS),
    )
    if exceeded != 0:
        raise BusinessError(
            ErrorCode.RATE_LIMITED,
            "Too many login attempts",
            http_status=429,
        )


async def clear_login_account_failures(
    redis: Redis,
    *,
    username: str,
) -> None:
    """登录成功后只清理账号维度；共享 IP 桶保留给其他账号。"""
    await redis.delete(_login_account_key(username))


async def build_auth_context(
    session: AsyncSession,
    user_id: int,
    snapshot: AuthSnapshot,
    *,
    redis: Redis | None = None,
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
        permission_codes=await get_role_permission_codes(
            session,
            redis or default_redis_client,
            role,
        ),
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
    "clear_login_account_failures",
    "commit_security_change",
    "enforce_login_rate_limit",
    "get_auth_snapshot",
    "invalidate_auth_snapshot",
    "invalidate_refresh_tokens",
    "invalidate_user_sessions",
    "issue_token_pair",
    "login_local_user",
    "logout_refresh_token",
    "rotate_refresh_token",
)
