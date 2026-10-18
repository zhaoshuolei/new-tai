"""跟进方式、跟进记录与附件测试的数据构造函数。

签名在此固定，其他测试只能从本模块导入，不得各自复制。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import ContactStatus, CustomerContact
from app.models.follow_up import (
    ContactFollowUp,
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)
from app.models.user import User


async def add_method(
    session: AsyncSession,
    *,
    name: str | None = None,
    status: ContactStatus = ContactStatus.ACTIVE,
) -> ContactFollowUpMethod:
    """写入跟进方式；生成列 active_name 交由数据库计算。"""
    method = ContactFollowUpMethod(
        name=name if name is not None else f"跟进方式-{uuid4().hex[:8]}",
        status=status,
    )
    session.add(method)
    await session.flush()
    return method


async def add_follow_up(
    session: AsyncSession,
    contact: CustomerContact,
    method: ContactFollowUpMethod,
    *,
    follow_up_user: User,
    content: str | None = None,
    follow_up_at: datetime | None = None,
    next_follow_up_at: datetime | None = None,
    status: ContactStatus = ContactStatus.ACTIVE,
) -> ContactFollowUp:
    """写入跟进记录，默认录入人与跟进人相同。"""
    follow_up = ContactFollowUp(
        contact_id=contact.id,
        method_id=method.id,
        follow_up_user_id=follow_up_user.id,
        follow_up_at=(
            follow_up_at if follow_up_at is not None else datetime(2026, 10, 1, 2, 0)
        ),
        content=(content if content is not None else f"跟进内容-{uuid4().hex[:8]}"),
        next_follow_up_at=next_follow_up_at,
        status=status,
        created_by=follow_up_user.id,
    )
    session.add(follow_up)
    await session.flush()
    return follow_up


async def add_attachment(
    session: AsyncSession,
    follow_up: ContactFollowUp,
    *,
    storage_path: str | None = None,
    original_name: str | None = None,
    content_type: str = "application/pdf",
    size_bytes: int = 1,
) -> ContactFollowUpAttachment:
    """写入跟进记录附件。"""
    attachment = ContactFollowUpAttachment(
        follow_up_id=follow_up.id,
        storage_path=(
            storage_path
            if storage_path is not None
            else f"contact-follow-up/1/{uuid4().hex}.pdf"
        ),
        original_name=(
            original_name
            if original_name is not None
            else f"附件-{uuid4().hex[:8]}.pdf"
        ),
        content_type=content_type,
        size_bytes=size_bytes,
        created_by=follow_up.created_by,
    )
    session.add(attachment)
    await session.flush()
    return attachment


__all__: tuple[str, ...] = (
    "add_attachment",
    "add_follow_up",
    "add_method",
)
