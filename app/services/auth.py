"""本地认证与令牌生命周期。"""

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.feishu import FeishuClient, FeishuUserInfo
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import key
from app.core.redis import redis_client as default_redis_client
from app.core.security import create_access_token, verify_password
from app.models.department import Department
from app.models.role import DataScope, Role, RoleDepartment, RoleStatus
from app.models.user import User, UserStatus
from app.repositories import role as role_repository
from app.repositories import user as user_repository

REFRESH_TOKEN_TTL_SECONDS = settings.refresh_token_expire_days * 24 * 60 * 60
LOGIN_RATE_LIMIT_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64
_PENDING_ASSIGNMENT_DEPARTMENT_NAME = "待分配"

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


def _refresh_key(user_id: int, device_id: str) -> str:
    return key("auth", "refresh", str(user_id), device_id)


def _refresh_key_pattern(user_id: int) -> str:
    return key("auth", "refresh", str(user_id), "*")


def normalize_username(username: str) -> str:
    """规范化并校验用户名，返回用于存储和查询的规范值。"""
    normalized = username.strip().casefold()
    if not normalized:
        raise ValueError("Username cannot be empty after normalization")
    if len(normalized) < USERNAME_MIN_LENGTH:
        raise ValueError(
            f"Username must be at least {USERNAME_MIN_LENGTH} characters "
            "after normalization"
        )
    if len(normalized) > USERNAME_MAX_LENGTH:
        raise ValueError(
            f"Username must be at most {USERNAME_MAX_LENGTH} characters "
            "after normalization"
        )
    return normalized


def _login_ip_key(client_ip: str) -> str:
    return key("auth", "login", "ip", client_ip)


def _login_account_key(username: str) -> str:
    return key("auth", "login", "account", normalize_username(username))


async def invalidate_refresh_tokens(redis: Redis, user_id: int) -> None:
    pattern = _refresh_key_pattern(user_id)
    async for refresh_key in redis.scan_iter(match=pattern):
        await redis.delete(refresh_key)


async def _invalidate_refresh_tokens_best_effort(
    redis: Redis,
    user_id: int,
) -> None:
    try:
        await invalidate_refresh_tokens(redis, user_id)
    except Exception:
        logger.exception(
            "Post-commit refresh token invalidation failed",
            extra={"user_id": user_id},
        )


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
    """在同一事务提交安全变更和 token_version，随后清理 refresh token。

    访问令牌每次请求都回源数据库并比对 token_version，因此旧访问令牌
    在提交后立即失效，无需依赖任何授权缓存或缓存失效操作。
    """
    if increment_token_version:
        await _increment_token_version_in_transaction(session, user_id)

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    await _invalidate_refresh_tokens_best_effort(redis, user_id)


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
    try:
        normalized_username = normalize_username(username)
    except ValueError as error:
        raise BusinessError(
            ErrorCode.INVALID_CREDENTIALS,
            "Invalid username or password",
            http_status=401,
        ) from error

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
    try:
        normalized_username = normalize_username(username)
    except ValueError as error:
        raise BusinessError(
            ErrorCode.INVALID_CREDENTIALS,
            "Invalid username or password",
            http_status=401,
        ) from error

    await enforce_login_rate_limit(
        redis,
        client_ip=client_ip,
        username=normalized_username,
    )
    user = await authenticate_local_user(session, normalized_username, password)
    await clear_login_account_failures(redis, username=normalized_username)
    tokens = await issue_token_pair(redis, user, device_id=device_id)
    return LoginResult(tokens=tokens, user=user)


async def _get_feishu_user_by_open_id(
    session: AsyncSession,
    open_id: str,
) -> User | None:
    """按飞书 open_id 查询本系统用户，供首次登录与竞争恢复复用。"""
    statement = select(User).where(User.feishu_open_id == open_id)
    return await session.scalar(statement)


async def _get_pending_department(session: AsyncSession) -> Department:
    """定位唯一的系统“待分配”节点，避免误选其他系统部门。"""
    statement = (
        select(Department)
        .where(
            Department.is_system.is_(True),
            Department.name == _PENDING_ASSIGNMENT_DEPARTMENT_NAME,
        )
        .order_by(Department.id)
        .limit(1)
    )
    department = await session.scalar(statement)
    if department is None:
        raise BusinessError(
            ErrorCode.INTERNAL_ERROR,
            "Pending assignment department is not configured",
            http_status=500,
        )
    return department


async def _get_default_role(session: AsyncSession) -> Role:
    role = await role_repository.get_by_code(session, "default")
    if role is None:
        raise BusinessError(
            ErrorCode.INTERNAL_ERROR,
            "Default role is not configured",
            http_status=500,
        )
    return role


def _sync_feishu_profile(
    user: User,
    info: FeishuUserInfo,
    *,
    login_at: datetime,
) -> None:
    if not user.nickname and info.name:
        user.nickname = info.name
    if not user.email and info.email:
        user.email = info.email
    if info.avatar_url:
        user.avatar = info.avatar_url
    user.last_login_at = login_at


def _prepare_existing_feishu_user(
    user: User,
    info: FeishuUserInfo,
    *,
    login_at: datetime,
) -> None:
    if user.status == UserStatus.DISABLED:
        raise BusinessError(
            ErrorCode.ACCOUNT_DISABLED,
            "Account disabled",
            http_status=401,
        )
    _sync_feishu_profile(user, info, login_at=login_at)


async def login_with_feishu(
    session: AsyncSession,
    redis: Redis,
    code: str,
    *,
    client_ip: str,
    client: FeishuClient | None = None,
) -> LoginResult:
    await enforce_login_ip_rate_limit(redis, client_ip=client_ip)
    feishu_client = client or FeishuClient()
    user_access_token = await feishu_client.exchange_code(code)
    info = await feishu_client.get_user_info(user_access_token)

    user = await _get_feishu_user_by_open_id(session, info.open_id)
    login_at = datetime.now(UTC).replace(tzinfo=None)

    if user is None:
        pending_department = await _get_pending_department(session)
        default_role = await _get_default_role(session)
        user = User(
            feishu_open_id=info.open_id,
            username=None,
            password_hash=None,
            nickname=info.name,
            email=info.email,
            avatar=info.avatar_url,
            department_id=pending_department.id,
            role_id=default_role.id,
            status=UserStatus.ACTIVE,
            last_login_at=login_at,
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            user = await _get_feishu_user_by_open_id(session, info.open_id)
            if user is None:
                raise BusinessError(
                    ErrorCode.FEISHU_UPSTREAM_ERROR,
                    "Feishu user provisioning conflict",
                    http_status=502,
                ) from None
            _prepare_existing_feishu_user(user, info, login_at=login_at)
            await session.commit()
    else:
        _prepare_existing_feishu_user(user, info, login_at=login_at)
        await session.commit()

    await session.refresh(user)
    tokens = await issue_token_pair(redis, user)
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


async def _enforce_login_rate_limit(
    redis: Redis,
    keys: tuple[str, ...],
) -> None:
    exceeded = await redis.eval(
        _LOGIN_RATE_LIMIT_SCRIPT,
        len(keys),
        *keys,
        str(LOGIN_RATE_LIMIT_WINDOW_SECONDS),
        str(LOGIN_RATE_LIMIT_ATTEMPTS),
    )
    if exceeded != 0:
        raise BusinessError(
            ErrorCode.RATE_LIMITED,
            "Too many login attempts",
            http_status=429,
        )


async def enforce_login_rate_limit(
    redis: Redis,
    *,
    client_ip: str,
    username: str,
) -> None:
    """原子预占 IP 与账号两个限流桶，超过阈值时拒绝登录。"""
    await _enforce_login_rate_limit(
        redis,
        (_login_ip_key(client_ip), _login_account_key(username)),
    )


async def enforce_login_ip_rate_limit(
    redis: Redis,
    *,
    client_ip: str,
) -> None:
    """仅按 IP 预占登录限流桶，供无本地账号的登录方式复用。"""
    await _enforce_login_rate_limit(redis, (_login_ip_key(client_ip),))


async def clear_login_account_failures(
    redis: Redis,
    *,
    username: str,
) -> None:
    """登录成功后只清理账号维度；共享 IP 桶保留给其他账号。"""
    await redis.delete(_login_account_key(username))


async def build_auth_context(
    session: AsyncSession,
    user: User,
) -> AuthContext:
    """从权威用户行构建请求授权上下文，不读取任何授权缓存。"""
    role = None
    if user.role_id is not None:
        role = await role_repository.get_by_id(session, user.role_id)

    if role is None or role.status == RoleStatus.DISABLED:
        return AuthContext(
            user_id=user.id,
            role_id=user.role_id,
            department_id=user.department_id,
            data_scope=DataScope.SELF,
            custom_department_ids=frozenset(),
            include_children=False,
            permission_codes=frozenset(),
            is_super_admin=False,
            username=user.username,
            nickname=user.nickname,
        )

    custom_department_ids: frozenset[int] = frozenset()
    if role.data_scope == DataScope.CUSTOM:
        result = await session.scalars(
            select(RoleDepartment.c.department_id).where(
                RoleDepartment.c.role_id == role.id
            )
        )
        custom_department_ids = frozenset(result.all())

    from app.services.role import list_role_permission_codes

    return AuthContext(
        user_id=user.id,
        role_id=role.id,
        department_id=user.department_id,
        data_scope=role.data_scope,
        custom_department_ids=custom_department_ids,
        include_children=role.data_scope_include_children,
        permission_codes=await list_role_permission_codes(session, role),
        is_super_admin=role.code == "super_admin" and role.is_system,
        username=user.username,
        nickname=user.nickname,
    )


__all__: tuple[str, ...] = (
    "LOGIN_RATE_LIMIT_ATTEMPTS",
    "LOGIN_RATE_LIMIT_WINDOW_SECONDS",
    "REFRESH_TOKEN_TTL_SECONDS",
    "LoginResult",
    "TokenPair",
    "authenticate_local_user",
    "build_auth_context",
    "bump_token_version",
    "clear_login_account_failures",
    "commit_security_change",
    "enforce_login_rate_limit",
    "invalidate_refresh_tokens",
    "issue_token_pair",
    "login_local_user",
    "login_with_feishu",
    "logout_refresh_token",
    "normalize_username",
    "rotate_refresh_token",
)
