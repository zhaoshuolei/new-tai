"""用户业务逻辑。"""

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.core.password_policy import validate_password_policy
from app.core.redis import redis_scope
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User, UserStatus
from app.repositories import customer as customer_repository
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
from app.services.audit import audited
from app.services.data_scope import apply_user_scope, build_user_scope_filter


def _username_exists_error() -> BusinessError:
    return BusinessError(
        ErrorCode.USERNAME_EXISTS,
        "Username already exists",
        http_status=409,
    )


def _identity_exists_error() -> BusinessError:
    return BusinessError(
        ErrorCode.CONFLICT,
        "Feishu identity already exists",
        http_status=409,
    )


def _restore_reference_error(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CONFLICT,
        message,
        http_status=409,
    )


def _validate_local_password(password: str) -> None:
    try:
        validate_password_policy(password)
    except ValueError as error:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            str(error),
            http_status=422,
        ) from error


def _validate_local_username(username: str) -> str:
    try:
        return auth_service.normalize_username(username)
    except ValueError as error:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            str(error),
            http_status=422,
        ) from error


@audited(action="user:create", resource_type="user", resource_id_attr="id")
async def create_local_user(
    session: AsyncSession,
    payload: UserCreate,
    *,
    context: AuthContext | None = None,
) -> User:
    _validate_local_password(payload.password)
    normalized_username = _validate_local_username(payload.username)
    existing = await user_repository.get_by_username(session, normalized_username)
    if existing is not None:
        raise _username_exists_error()

    user = User(
        feishu_open_id=None,
        username=normalized_username,
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
    *,
    deleted: bool = False,
) -> User | None:
    deletion_filter = (
        User.deleted_at.is_not(None) if deleted else User.deleted_at.is_(None)
    )
    statement = apply_user_scope(
        select(User).where(
            User.id == user_id,
            deletion_filter,
        ),
        context,
    )
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
    *,
    for_update: bool = False,
) -> User:
    user = (
        await user_repository.lock_by_id(session, user_id)
        if for_update
        else await user_repository.get_by_id(session, user_id)
    )
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )
    await authorization_service.assert_target_visible(session, context, user)
    return user


async def _get_current_role(
    session: AsyncSession,
    user: User,
) -> Role | None:
    if user.role_id is None:
        return None
    return await role_repository.get_by_id(session, user.role_id)


async def _commit_user_security_change(
    session: AsyncSession,
    user_id: int,
    redis: Redis | None,
    *,
    increment_token_version: bool = True,
) -> None:
    async with redis_scope(redis) as resolved_redis:
        await auth_service.commit_security_change(
            session,
            user_id,
            redis=resolved_redis,
            increment_token_version=increment_token_version,
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


async def list_deleted_users(
    session: AsyncSession,
    *,
    context: AuthContext,
    page: int,
    page_size: int,
) -> tuple[list[User], int]:
    return await user_repository.list_deleted_users(
        session,
        page=page,
        page_size=page_size,
        scope_filter=build_user_scope_filter(context),
    )


@audited(action="user:update", resource_type="user", resource_id_param="user_id")
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


@audited(action="user:assign", resource_type="user", resource_id_param="user_id")
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
        target_role = await role_repository.lock_by_id(
            session,
            payload.role_id,
            for_share=True,
        )
        if target_role is None:
            raise BusinessError(
                ErrorCode.ROLE_NOT_FOUND,
                "Role not found",
                http_status=404,
            )
        authorization_service.assert_can_assign_role(context, target_role)

    await authorization_service.assert_can_revoke_super_admin(
        session,
        context,
        user,
        current_role,
        target_role,
    )

    if user.role_id == payload.role_id:
        return user

    user.role_id = payload.role_id
    await _commit_user_security_change(session, user.id, redis)
    await session.refresh(user)
    return user


@audited(action="user:assign", resource_type="user", resource_id_param="user_id")
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
        department = await department_repository.lock_by_id(
            session,
            payload.department_id,
            for_share=True,
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
    await _commit_user_security_change(session, user.id, redis)
    await session.refresh(user)
    return user


@audited(action="user:update", resource_type="user", resource_id_param="user_id")
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
    current_role = await _get_current_role(session, user)
    await authorization_service.assert_can_change_super_admin_status(
        session,
        context,
        user,
        current_role,
        payload.status,
    )

    if user.status == payload.status:
        return user

    user.status = payload.status
    await _commit_user_security_change(session, user.id, redis)
    await session.refresh(user)
    return user


@audited(action="user:update", resource_type="user", resource_id_param="user_id")
async def reset_user_password(
    session: AsyncSession,
    user_id: int,
    payload: UserPasswordReset,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> User:
    user = await _get_writable_user(session, user_id, context)
    current_role = await _get_current_role(session, user)
    authorization_service.assert_can_reset_password(context, user, current_role)
    if user.feishu_open_id is not None:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Password reset is only available for local accounts",
        )
    _validate_local_password(payload.new_password)

    user.password_hash = hash_password(payload.new_password)
    await _commit_user_security_change(session, user.id, redis)
    await session.refresh(user)
    return user


@audited(action="user:delete", resource_type="user", resource_id_param="user_id")
async def delete_user(
    session: AsyncSession,
    user_id: int,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> None:
    user = await _get_writable_user(
        session,
        user_id,
        context,
        for_update=True,
    )
    authorization_service.assert_not_self(context, user)
    current_role = await _get_current_role(session, user)
    await authorization_service.assert_can_revoke_super_admin(
        session,
        context,
        user,
        current_role,
        None,
        "delete",
    )
    if await customer_repository.has_customers(session, user_id):
        raise BusinessError(
            ErrorCode.USER_HAS_CUSTOMERS,
            "User still owns customers; transfer those customers first",
            http_status=409,
        )

    try:
        async with session.begin_nested():
            await user_repository.soft_delete(
                session,
                user,
                deleted_by=context.user_id,
            )
            await session.flush()
            if await customer_repository.has_customers_for_update(session, user_id):
                raise BusinessError(
                    ErrorCode.USER_HAS_CUSTOMERS,
                    "User still owns customers; transfer those customers first",
                    http_status=409,
                )
    except BusinessError:
        await session.refresh(user)
        raise
    await _commit_user_security_change(session, user_id, redis)


@audited(
    action="user:restore",
    resource_type="user",
    resource_id_param="user_id",
)
async def restore_user(
    session: AsyncSession,
    user_id: int,
    *,
    context: AuthContext,
    redis: Redis | None = None,
) -> User:
    user = await _get_scoped_user(session, user_id, context, deleted=True)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )
    restored_role: Role | None = None
    if user.role_id is not None:
        restored_role = await role_repository.lock_by_id(
            session,
            user.role_id,
            for_share=True,
        )
        if restored_role is None:
            raise _restore_reference_error("Assigned role is unavailable")
        authorization_service.assert_can_manage_super_admin_role(
            context,
            restored_role,
        )
    if user.department_id is not None:
        restored_department = await department_repository.lock_by_id(
            session,
            user.department_id,
            for_share=True,
        )
        if restored_department is None:
            raise _restore_reference_error("Assigned department is unavailable")

    username = user.username
    feishu_open_id = user.feishu_open_id
    if username is not None:
        existing = await user_repository.get_by_username(session, username)
        if existing is not None and existing.id != user.id:
            raise _username_exists_error()
    if feishu_open_id is not None:
        existing = await user_repository.get_by_feishu_open_id(
            session,
            feishu_open_id,
        )
        if existing is not None and existing.id != user.id:
            raise _identity_exists_error()

    try:
        async with session.begin_nested():
            await user_repository.restore(session, user)
            await session.flush()
    except IntegrityError as error:
        message = str(error.orig).lower()
        if _is_duplicate_username(error):
            if "active_username" in message or "user.username" in message:
                raise _username_exists_error() from error
            if "active_feishu_open_id" in message or "user.feishu_open_id" in message:
                raise _identity_exists_error() from error
            if username is not None:
                raise _username_exists_error() from error
            if feishu_open_id is not None:
                raise _identity_exists_error() from error
        raise

    await _commit_user_security_change(session, user.id, redis)
    await session.refresh(user)
    return user


__all__: tuple[str, ...] = (
    "assign_user_department",
    "assign_user_role",
    "create_local_user",
    "delete_user",
    "get_user",
    "list_deleted_users",
    "list_users",
    "reset_user_password",
    "restore_user",
    "update_user",
    "update_user_status",
)
