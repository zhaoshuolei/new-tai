"""Current-user avatar application services."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.best_effort import run_best_effort
from app.core.errors import BusinessError, ErrorCode
from app.repositories import user as user_repository
from app.services.audit import audited
from app.services.system_setting_asset import (
    remove_user_avatar,
    upload_user_avatar,
)


@audited(
    action="user_profile:avatar_upload",
    resource_type="user_profile",
    resource_id_param="user_id",
)
async def upload_avatar(
    session: AsyncSession,
    *,
    user_id: int,
    content_type: str | None,
    chunks: AsyncIterator[bytes],
    context: AuthContext,
) -> str:
    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    previous_url = user.avatar
    url = await upload_user_avatar(
        content_type=content_type,
        chunks=chunks,
    )
    user.avatar = url
    try:
        await session.commit()
    except Exception:

        async def remove_failed_upload() -> None:
            await remove_user_avatar(url)

        await run_best_effort(
            remove_failed_upload,
            description="remove avatar from failed upload commit",
        )
        raise

    async def remove_previous() -> None:
        await remove_user_avatar(previous_url)

    await run_best_effort(
        remove_previous,
        description="remove superseded user avatar",
    )
    return url


@audited(
    action="user_profile:avatar_delete",
    resource_type="user_profile",
    resource_id_param="user_id",
)
async def delete_avatar(
    session: AsyncSession,
    *,
    user_id: int,
    context: AuthContext,
) -> None:
    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    previous_url = user.avatar
    user.avatar = None
    await session.commit()

    async def remove_previous() -> None:
        await remove_user_avatar(previous_url)

    await run_best_effort(
        remove_previous,
        description="remove deleted user avatar",
    )


__all__: tuple[str, ...] = (
    "delete_avatar",
    "upload_avatar",
)
