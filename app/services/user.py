"""用户业务逻辑。"""

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.core.redis import redis_scope
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User, UserStatus
from app.repositories import department as department_repository
from app.repositories import role as role_repository
from app.repositories import user as user_repository
from app.schemas.user import (
    UserCreate,
    UserDepartmentUpdate,
    UserPasswordReset,
    UserRoleUpdate,
    UserStatusUpdate,
    UserUpdate,
)
from app.services import auth as auth_service
from app.services import authorization as authorization_service
from app.services.data_scope import apply_user_scope, build_user_scope_filter


def _username_exists_error() -> BusinessError:
    return BusinessError(
        ErrorCode.USERNAME_EXISTS,
        "Username already exists",
        http_status=409,
    )


async def create_local_user(session: AsyncSession, payload: UserCreate) -> User:
    existing = await user_repository.get_by_username(session, payload.username)
    if existing is not None:
        raise _username_exists_error()

    user = User(
        feishu_open_id=None,
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname,
        email=payload.email,
        avatar=payload.avatar,
        department_id=None,
        role_id=None,
        status=UserStatus.ACTIVE,
        token_version=0,
        last_login_at=None,
    )
    try:
        await user_repository.add(session, user)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if _is_duplicate_username(error):
            raise _username_exists_error() from error
        raise

    await session.refresh(user)
    return user


def _is_duplicate_username(error: IntegrityError) -> bool:
    args = getattr(error.orig, "args", ())
    return bool(args and args[0] == 1062)


async def _get_scoped_user(
    session: AsyncSession,
    user_id: int,
    context: AuthContext,
) -> User | None:
    statement = apply_user_scope(select(User).where(User.id == user_id), context)
    user = await session.scalar(statement)
    return user if isinstance(user, User) else None


async def get_user(
    session: AsyncSession,
    user_id: int,
    *,
    context: AuthContext,
) -> User:
    user = await _get_scoped_user(session, user_id, context)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )
    return user


async def _get_writable_user(
    session: AsyncSession,
    user_id: int,
    context: AuthContext,
) -> User:
    user = await _get_scoped_user(session, user_id, context)
    if user is not None:
        return user

    if await user_repository.get_by_id(session, user_id) is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )
    raise BusinessError(
        ErrorCode.FORBIDDEN,
        "User is outside the current data scope",
        http_status=403,
    )


async def _get_current_role(
    session: AsyncSession,
    user: User,
) -> Role | None:
    if user.role_id is None:
        return None
    return await role_repository.get_by_id(session, user.role_id)


async def _bump_user_token_version(
    session: AsyncSession,
    user_id: int,
    redis: Redis | None,
) -> None:
    async with redis_scope(redis) as resolved_redis:
        await auth_service.bump_token_version(
            session,
            user_id,
            redis=resolved_redis,
        )


async def list_users(
    session: AsyncSession,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
    department_id: int | None = None,
    role_id: int | None = None,
    user_status: UserStatus | None = None,
    keyword: str | None = None,
) -> tuple[list[User], int]:
    return await user_repository.list_users(
        session,
        page=page,
        page_size=page_size,
        scope_filter=build_user_scope_filter(context),
        department_id=department_id,
        role_id=role_id,
        user_status=user_status,
        keyword=keyword,
    )


async def update_user(
    session: AsyncSession,
    user_id: int,
    payload: UserUpdate,
    *,
    context: AuthContext,
) -> User:
    user = await _get_writable_user(session, user_id, context)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field_name, value)

    await session.commit()
    await session.refresh(user)
    return user


async def assign_user_role(
    session: AsyncSession,
    user_id: int,
    payload: UserRoleUpdate,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> User:
    user = await _get_writable_user(session, user_id, context)
    authorization_service.assert_not_self(context, user)
    current_role = await _get_current_role(session, user)

    target_role: Role | None = None
    if payload.role_id is not None:
        target_role = await role_repository.get_by_id(session, payload.role_id)
        if target_role is None:
            raise BusinessError(
                ErrorCode.ROLE_NOT_FOUND,
                "Role not found",
                http_status=404,
            )
        authorization_service.assert_can_assign_role(context, target_role)

    if (
        current_role is not None
        and authorization_service.is_system_super_admin(current_role)
        and (target_role is None or target_role.id != current_role.id)
    ):
        if not context.is_super_admin:
            raise BusinessError(
                ErrorCode.CANNOT_GRANT_SUPER_ADMIN,
                "Only a super administrator can revoke super administrator",
                http_status=403,
            )
        await authorization_service.assert_can_reduce_super_admin(
            session,
            user,
            current_role,
            "demote",
        )

    if user.role_id == payload.role_id:
        return user

    user.role_id = payload.role_id
    await session.commit()
    await _bump_user_token_version(session, user.id, redis)
    await session.refresh(user)
    return user


async def assign_user_department(
    session: AsyncSession,
    user_id: int,
    payload: UserDepartmentUpdate,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> User:
    user = await _get_writable_user(session, user_id, context)
    authorization_service.assert_not_self(context, user)

    if payload.department_id is not None:
        department = await department_repository.get_by_id(
            session,
            payload.department_id,
        )
        if department is None:
            raise BusinessError(
                ErrorCode.DEPT_NOT_FOUND,
                "Department not found",
                http_status=404,
            )
        await authorization_service.assert_can_assign_department(
            session,
            context,
            department,
        )

    if user.department_id == payload.department_id:
        return user

    user.department_id = payload.department_id
    await session.commit()
    await _bump_user_token_version(session, user.id, redis)
    await session.refresh(user)
    return user


async def update_user_status(
    session: AsyncSession,
    user_id: int,
    payload: UserStatusUpdate,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> User:
    user = await _get_writable_user(session, user_id, context)
    authorization_service.assert_not_self(context, user)

    if user.status == UserStatus.ACTIVE and payload.status == UserStatus.DISABLED:
        current_role = await _get_current_role(session, user)
        await authorization_service.assert_can_reduce_super_admin(
            session,
            user,
            current_role,
            "disable",
        )

    if user.status == payload.status:
        return user

    user.status = payload.status
    await session.commit()
    await _bump_user_token_version(session, user.id, redis)
    await session.refresh(user)
    return user


async def reset_user_password(
    session: AsyncSession,
    user_id: int,
    payload: UserPasswordReset,
    *,
    context: AuthContext,
) -> User:
    user = await _get_writable_user(session, user_id, context)
    if user.feishu_open_id is not None:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Password reset is only available for local accounts",
        )

    user.password_hash = hash_password(payload.new_password)
    await session.commit()
    await session.refresh(user)
    return user


async def delete_user(
    session: AsyncSession,
    user_id: int,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> None:
    user = await _get_writable_user(session, user_id, context)
    authorization_service.assert_not_self(context, user)
    current_role = await _get_current_role(session, user)
    await authorization_service.assert_can_reduce_super_admin(
        session,
        user,
        current_role,
        "delete",
    )

    await user_repository.delete(session, user)
    await session.commit()

    async with redis_scope(redis) as resolved_redis:
        await auth_service.invalidate_user_sessions(resolved_redis, user_id)


__all__: tuple[str, ...] = (
    "assign_user_department",
    "assign_user_role",
    "create_local_user",
    "delete_user",
    "get_user",
    "list_users",
    "reset_user_password",
    "update_user",
    "update_user_status",
)
