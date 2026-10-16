"""用户业务逻辑。"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.core.security import hash_password
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


async def get_user(session: AsyncSession, user_id: int) -> User:
    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )
    return user


async def list_users(
    session: AsyncSession,
    *,
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
        department_id=department_id,
        role_id=role_id,
        user_status=user_status,
        keyword=keyword,
    )


async def update_user(
    session: AsyncSession,
    user_id: int,
    payload: UserUpdate,
) -> User:
    user = await get_user(session, user_id)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field_name, value)

    await session.commit()
    await session.refresh(user)
    return user


async def assign_user_role(
    session: AsyncSession,
    user_id: int,
    payload: UserRoleUpdate,
) -> User:
    user = await get_user(session, user_id)
    if payload.role_id is not None:
        role = await role_repository.get_by_id(session, payload.role_id)
        if role is None:
            raise BusinessError(
                ErrorCode.ROLE_NOT_FOUND,
                "Role not found",
                http_status=404,
            )

    user.role_id = payload.role_id
    await session.commit()
    await session.refresh(user)
    return user


async def assign_user_department(
    session: AsyncSession,
    user_id: int,
    payload: UserDepartmentUpdate,
) -> User:
    user = await get_user(session, user_id)
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

    user.department_id = payload.department_id
    await session.commit()
    await session.refresh(user)
    return user


async def update_user_status(
    session: AsyncSession,
    user_id: int,
    payload: UserStatusUpdate,
) -> User:
    user = await get_user(session, user_id)
    user.status = payload.status
    await session.commit()
    await session.refresh(user)
    return user


async def reset_user_password(
    session: AsyncSession,
    user_id: int,
    payload: UserPasswordReset,
) -> User:
    user = await get_user(session, user_id)
    if user.feishu_open_id is not None:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Password reset is only available for local accounts",
        )

    user.password_hash = hash_password(payload.new_password)
    await session.commit()
    await session.refresh(user)
    return user


async def delete_user(session: AsyncSession, user_id: int) -> None:
    user = await get_user(session, user_id)
    await user_repository.delete(session, user)
    await session.commit()


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
