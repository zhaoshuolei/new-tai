"""跟进方式、跟进记录与附件的数据访问。"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.models.contact import ContactStatus, CustomerContact
from app.models.follow_up import (
    ContactFollowUp,
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)
from app.models.user import User


@dataclass(frozen=True)
class FollowUpRow:
    """跟进记录及其列表展示所需字段。"""

    follow_up: ContactFollowUp
    contact_name: str
    method_name: str
    follow_up_user_nickname: str | None
    follow_up_user_username: str | None
    created_by_nickname: str | None
    created_by_username: str | None


async def get_method(
    session: AsyncSession,
    method_id: int,
) -> ContactFollowUpMethod | None:
    return await session.get(ContactFollowUpMethod, method_id)


async def lock_method_for_update(
    session: AsyncSession,
    method_id: int,
) -> ContactFollowUpMethod | None:
    statement = (
        select(ContactFollowUpMethod)
        .where(ContactFollowUpMethod.id == method_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def lock_method_shared(
    session: AsyncSession,
    method_id: int,
) -> ContactFollowUpMethod | None:
    statement = (
        select(ContactFollowUpMethod)
        .where(ContactFollowUpMethod.id == method_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def list_methods(
    session: AsyncSession,
    *,
    status: ContactStatus,
) -> list[ContactFollowUpMethod]:
    statement = (
        select(ContactFollowUpMethod)
        .where(ContactFollowUpMethod.status == status)
        .order_by(ContactFollowUpMethod.name, ContactFollowUpMethod.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def lock_follow_up_ids_referencing_method(
    session: AsyncSession,
    method_id: int,
) -> list[int]:
    """当前读取引用跟进方式的记录 ID，并锁住这些行。

    删除跟进方式不能使用普通 COUNT：REPEATABLE READ 的事务快照可能早于
    并发创建的跟进记录提交。这里不筛选状态，已删除记录也必须纳入引用检查。
    """
    statement = (
        select(ContactFollowUp.id)
        .where(ContactFollowUp.method_id == method_id)
        .order_by(ContactFollowUp.id)
        .with_for_update()
    )
    result = await session.scalars(statement)
    return list(result.all())


async def add_follow_up(
    session: AsyncSession,
    follow_up: ContactFollowUp,
) -> ContactFollowUp:
    session.add(follow_up)
    await session.flush()
    # 刷新服务端生成的 created_at/updated_at，避免后续异步序列化触发
    # MissingGreenlet。
    await session.refresh(follow_up)
    return follow_up


async def get_follow_up(
    session: AsyncSession,
    contact_id: int,
    follow_up_id: int,
) -> ContactFollowUp | None:
    statement = select(ContactFollowUp).where(
        ContactFollowUp.contact_id == contact_id,
        ContactFollowUp.id == follow_up_id,
    )
    return await session.scalar(statement)


async def lock_follow_up_for_update(
    session: AsyncSession,
    contact_id: int,
    follow_up_id: int,
) -> ContactFollowUp | None:
    statement = (
        select(ContactFollowUp)
        .where(
            ContactFollowUp.contact_id == contact_id,
            ContactFollowUp.id == follow_up_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def list_follow_ups(
    session: AsyncSession,
    contact_id: int,
    *,
    status: ContactStatus,
    follow_up_user_id: int | None,
    page: int,
    page_size: int,
) -> tuple[list[FollowUpRow], int]:
    conditions: list[ColumnElement[bool]] = [
        ContactFollowUp.contact_id == contact_id,
        ContactFollowUp.status == status,
    ]
    if follow_up_user_id is not None:
        conditions.append(ContactFollowUp.follow_up_user_id == follow_up_user_id)

    total_statement = (
        select(func.count()).select_from(ContactFollowUp).where(*conditions)
    )
    total = await session.scalar(total_statement)

    follow_up_user = aliased(User)
    created_by_user = aliased(User)
    statement = (
        select(
            ContactFollowUp,
            CustomerContact.name,
            ContactFollowUpMethod.name,
            follow_up_user.nickname,
            follow_up_user.username,
            created_by_user.nickname,
            created_by_user.username,
        )
        .join(CustomerContact, ContactFollowUp.contact_id == CustomerContact.id)
        .join(
            ContactFollowUpMethod,
            ContactFollowUp.method_id == ContactFollowUpMethod.id,
        )
        .join(
            follow_up_user,
            ContactFollowUp.follow_up_user_id == follow_up_user.id,
        )
        .join(
            created_by_user,
            ContactFollowUp.created_by == created_by_user.id,
        )
        .where(*conditions)
        .order_by(ContactFollowUp.follow_up_at.desc(), ContactFollowUp.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(statement)
    rows = [
        FollowUpRow(
            follow_up=follow_up,
            contact_name=contact_name,
            method_name=method_name,
            follow_up_user_nickname=follow_up_user_nickname,
            follow_up_user_username=follow_up_user_username,
            created_by_nickname=created_by_nickname,
            created_by_username=created_by_username,
        )
        for (
            follow_up,
            contact_name,
            method_name,
            follow_up_user_nickname,
            follow_up_user_username,
            created_by_nickname,
            created_by_username,
        ) in result.all()
    ]
    return rows, int(total or 0)


async def get_follow_up_row(
    session: AsyncSession,
    follow_up_id: int,
) -> FollowUpRow | None:
    follow_up_user = aliased(User)
    created_by_user = aliased(User)
    statement = (
        select(
            ContactFollowUp,
            CustomerContact.name,
            ContactFollowUpMethod.name,
            follow_up_user.nickname,
            follow_up_user.username,
            created_by_user.nickname,
            created_by_user.username,
        )
        .join(CustomerContact, ContactFollowUp.contact_id == CustomerContact.id)
        .join(
            ContactFollowUpMethod,
            ContactFollowUp.method_id == ContactFollowUpMethod.id,
        )
        .join(
            follow_up_user,
            ContactFollowUp.follow_up_user_id == follow_up_user.id,
        )
        .join(
            created_by_user,
            ContactFollowUp.created_by == created_by_user.id,
        )
        .where(ContactFollowUp.id == follow_up_id)
    )
    result = await session.execute(statement)
    row = result.one_or_none()
    if row is None:
        return None
    (
        follow_up,
        contact_name,
        method_name,
        follow_up_user_nickname,
        follow_up_user_username,
        created_by_nickname,
        created_by_username,
    ) = row
    return FollowUpRow(
        follow_up=follow_up,
        contact_name=contact_name,
        method_name=method_name,
        follow_up_user_nickname=follow_up_user_nickname,
        follow_up_user_username=follow_up_user_username,
        created_by_nickname=created_by_nickname,
        created_by_username=created_by_username,
    )


async def count_attachments(
    session: AsyncSession,
    follow_up_id: int,
) -> int:
    statement = (
        select(func.count())
        .select_from(ContactFollowUpAttachment)
        .where(ContactFollowUpAttachment.follow_up_id == follow_up_id)
    )
    total = await session.scalar(statement)
    return int(total or 0)


async def list_attachments(
    session: AsyncSession,
    follow_up_ids: list[int],
) -> dict[int, list[ContactFollowUpAttachment]]:
    if not follow_up_ids:
        return {}
    statement = (
        select(ContactFollowUpAttachment)
        .where(ContactFollowUpAttachment.follow_up_id.in_(follow_up_ids))
        .order_by(
            ContactFollowUpAttachment.follow_up_id,
            ContactFollowUpAttachment.id,
        )
    )
    result = await session.scalars(statement)
    grouped: dict[int, list[ContactFollowUpAttachment]] = {}
    for attachment in result.all():
        grouped.setdefault(attachment.follow_up_id, []).append(attachment)
    return grouped


async def get_attachment(
    session: AsyncSession,
    follow_up_id: int,
    attachment_id: int,
) -> ContactFollowUpAttachment | None:
    statement = select(ContactFollowUpAttachment).where(
        ContactFollowUpAttachment.follow_up_id == follow_up_id,
        ContactFollowUpAttachment.id == attachment_id,
    )
    return await session.scalar(statement)


async def add_attachment(
    session: AsyncSession,
    attachment: ContactFollowUpAttachment,
) -> ContactFollowUpAttachment:
    session.add(attachment)
    await session.flush()
    # 附件响应会读取服务端生成的时间字段，刷新后避免异步懒加载。
    await session.refresh(attachment)
    return attachment


async def delete_attachment_row(
    session: AsyncSession,
    attachment: ContactFollowUpAttachment,
) -> None:
    await session.delete(attachment)
    await session.flush()


__all__: tuple[str, ...] = (
    "FollowUpRow",
    "add_attachment",
    "add_follow_up",
    "count_attachments",
    "delete_attachment_row",
    "get_attachment",
    "get_follow_up",
    "get_follow_up_row",
    "get_method",
    "list_attachments",
    "list_follow_ups",
    "list_methods",
    "lock_follow_up_for_update",
    "lock_follow_up_ids_referencing_method",
    "lock_method_for_update",
    "lock_method_shared",
)
