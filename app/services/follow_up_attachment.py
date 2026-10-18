"""跟进记录附件：权限校验、私有文件持久化与数据库记录维护。"""

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.best_effort import run_best_effort
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.follow_up import ContactFollowUp, ContactFollowUpAttachment
from app.repositories import follow_up as follow_up_repository
from app.services.audit import audited
from app.services.follow_up import (
    _follow_up_not_found_error,
    require_active_contact,
    require_active_follow_up,
    require_follow_up_owner,
    require_visible_active_customer,
)
from app.services.follow_up_attachment_storage import (
    remove_attachment_file,
    resolve_attachment_path,
    sanitize_original_name,
    store_attachment,
)


def _attachment_not_found_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND,
        "Follow-up attachment not found",
        http_status=404,
    )


def _attachment_limit_error() -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_ATTACHMENT_LIMIT,
        "Follow-up record attachment limit reached",
        http_status=422,
    )


async def _require_visible_active_follow_up(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    *,
    context: AuthContext,
) -> ContactFollowUp:
    await require_visible_active_customer(session, customer_id, context=context)
    await require_active_contact(session, customer_id, contact_id)
    return await require_active_follow_up(session, contact_id, follow_up_id)


async def _require_owned_active_follow_up(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    *,
    context: AuthContext,
) -> ContactFollowUp:
    follow_up = await _require_visible_active_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        context=context,
    )
    require_follow_up_owner(follow_up, context=context)
    return follow_up


async def _lock_owned_active_follow_up(
    session: AsyncSession,
    contact_id: int,
    follow_up_id: int,
    *,
    context: AuthContext,
) -> ContactFollowUp:
    follow_up = await follow_up_repository.lock_follow_up_for_update(
        session,
        contact_id,
        follow_up_id,
    )
    if follow_up is None or follow_up.status != ContactStatus.ACTIVE:
        raise _follow_up_not_found_error()
    require_follow_up_owner(follow_up, context=context)
    return follow_up


@audited(
    action="contact_follow_up:attachment_upload",
    resource_type="contact_follow_up_attachment",
    resource_id_attr="id",
)
async def upload_attachment(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    *,
    content_type: str | None,
    original_name: str | None,
    chunks: AsyncIterator[bytes],
    context: AuthContext,
) -> ContactFollowUpAttachment:
    """校验记录归属后在锁内限制数量，持久化文件并新增附件记录。"""
    await _require_owned_active_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        context=context,
    )
    await _lock_owned_active_follow_up(
        session,
        contact_id,
        follow_up_id,
        context=context,
    )

    count = await follow_up_repository.count_attachments(session, follow_up_id)
    if count >= settings.follow_up_attachment_max_per_record:
        raise _attachment_limit_error()

    stored = await store_attachment(
        follow_up_id=follow_up_id,
        content_type=content_type,
        chunks=chunks,
    )
    attachment = ContactFollowUpAttachment(
        follow_up_id=follow_up_id,
        storage_path=stored.storage_path,
        original_name=sanitize_original_name(original_name),
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        created_by=context.user_id,
    )
    try:
        await follow_up_repository.add_attachment(session, attachment)
        await session.commit()
    except Exception:

        async def remove_failed_upload() -> None:
            await remove_attachment_file(stored.storage_path)

        await run_best_effort(
            remove_failed_upload,
            description="remove follow-up attachment from failed upload",
        )
        raise
    return attachment


async def get_attachment_for_download(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    attachment_id: int,
    *,
    context: AuthContext,
) -> tuple[Path, ContactFollowUpAttachment]:
    """返回可见记录附件及私有根目录内的安全文件路径。"""
    await _require_visible_active_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        context=context,
    )
    attachment = await follow_up_repository.get_attachment(
        session,
        follow_up_id,
        attachment_id,
    )
    if attachment is None:
        raise _attachment_not_found_error()

    path = resolve_attachment_path(attachment.storage_path)
    if path is None or not path.is_file():
        raise _attachment_not_found_error()
    return path, attachment


@audited(
    action="contact_follow_up:attachment_delete",
    resource_type="contact_follow_up_attachment",
    resource_id_param="attachment_id",
)
async def delete_attachment(
    session: AsyncSession,
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    attachment_id: int,
    *,
    context: AuthContext,
) -> None:
    """仅当前跟进人可删除附件记录，提交后尽力删除私有文件。"""
    await _require_owned_active_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        context=context,
    )
    await _lock_owned_active_follow_up(
        session,
        contact_id,
        follow_up_id,
        context=context,
    )
    attachment = await follow_up_repository.get_attachment(
        session,
        follow_up_id,
        attachment_id,
    )
    if attachment is None:
        raise _attachment_not_found_error()

    storage_path = attachment.storage_path
    await follow_up_repository.delete_attachment_row(session, attachment)
    await session.commit()

    async def remove_deleted_attachment() -> None:
        await remove_attachment_file(storage_path)

    await run_best_effort(
        remove_deleted_attachment,
        description="remove deleted follow-up attachment",
    )


__all__: tuple[str, ...] = (
    "delete_attachment",
    "get_attachment_for_download",
    "upload_attachment",
)
