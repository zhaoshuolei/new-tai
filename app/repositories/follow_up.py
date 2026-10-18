"""跟进方式、跟进记录与附件的数据访问。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from app.models.contact import ContactFunction, ContactStatus, CustomerContact
from app.models.customer import Customer
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

    @property
    def id(self) -> int:
        """暴露记录 ID，供审计采集资源标识使用。"""
        return self.follow_up.id


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
    """锁定并统计跟进记录附件数。

    REPEATABLE READ 快照会让锁内普通 COUNT 读到并发提交前的旧值，这里必须使用
    `FOR UPDATE` 当前读。它与附件写入的“先记录行锁、后计数”加锁顺序配套，
    确保并发上传不能突破数量上限。
    """
    statement = (
        select(func.count())
        .select_from(ContactFollowUpAttachment)
        .where(ContactFollowUpAttachment.follow_up_id == follow_up_id)
        .with_for_update()
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


# 待跟进查询：当前计划候选、列表、客户聚合与状态计数。

DueState = Literal["today", "overdue", "all"]


@dataclass(frozen=True)
class DueFollowUpRow:
    """当前待跟进计划及其联系人、客户和跟进人展示字段。"""

    follow_up: ContactFollowUp
    contact: CustomerContact
    function_name: str
    customer: Customer
    follow_up_user_nickname: str | None
    follow_up_user_username: str | None
    due_state: Literal["today", "overdue"]


@dataclass(frozen=True)
class DueCustomerRow:
    """客户维度的待跟进聚合。"""

    customer_id: int
    customer_name: str
    due_count: int
    overdue_count: int
    earliest_next_follow_up_at: datetime


def _current_plan_candidates() -> Subquery:
    """为每个对接人选出全部未删除记录中最新的一条。"""
    return (
        select(
            ContactFollowUp.id.label("follow_up_id"),
            ContactFollowUp.contact_id.label("contact_id"),
            ContactFollowUp.follow_up_user_id.label("follow_up_user_id"),
            ContactFollowUp.next_follow_up_at.label("next_follow_up_at"),
            func.row_number()
            .over(
                partition_by=ContactFollowUp.contact_id,
                order_by=(
                    ContactFollowUp.follow_up_at.desc(),
                    ContactFollowUp.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(ContactFollowUp.status == ContactStatus.ACTIVE)
        .subquery("current_plan_candidates")
    )


def _due_conditions(
    candidate: Subquery,
    *,
    scope_filter: ColumnElement[bool],
    due_state: DueState,
    today_start: datetime,
    tomorrow_start: datetime,
    follow_up_user_id: int | None,
) -> list[ColumnElement[bool]]:
    """构造待跟进候选的共享过滤条件。"""
    next_follow_up_at = candidate.c.next_follow_up_at
    conditions: list[ColumnElement[bool]] = [
        candidate.c.row_number == 1,
        next_follow_up_at.is_not(None),
        CustomerContact.status == ContactStatus.ACTIVE,
        Customer.status == ContactStatus.ACTIVE,
        scope_filter,
    ]

    if due_state == "overdue":
        conditions.append(next_follow_up_at < today_start)
    elif due_state == "today":
        conditions.extend(
            (
                next_follow_up_at >= today_start,
                next_follow_up_at < tomorrow_start,
            )
        )
    else:
        conditions.append(next_follow_up_at < tomorrow_start)

    if follow_up_user_id is not None:
        conditions.append(candidate.c.follow_up_user_id == follow_up_user_id)
    return conditions


async def list_due_follow_ups(
    session: AsyncSession,
    *,
    scope_filter: ColumnElement[bool],
    due_state: DueState,
    today_start: datetime,
    tomorrow_start: datetime,
    follow_up_user_id: int | None,
    page: int,
    page_size: int,
) -> tuple[list[DueFollowUpRow], int]:
    """分页列出当前计划已到期或今日到期的对接人。"""
    candidate = _current_plan_candidates()
    conditions = _due_conditions(
        candidate,
        scope_filter=scope_filter,
        due_state=due_state,
        today_start=today_start,
        tomorrow_start=tomorrow_start,
        follow_up_user_id=follow_up_user_id,
    )

    total_statement = (
        select(func.count())
        .select_from(candidate)
        .join(
            CustomerContact,
            candidate.c.contact_id == CustomerContact.id,
        )
        .join(Customer, CustomerContact.customer_id == Customer.id)
        .where(*conditions)
    )
    total = await session.scalar(total_statement)

    statement = (
        select(
            ContactFollowUp,
            CustomerContact,
            ContactFunction.name,
            Customer,
            User.nickname,
            User.username,
            case(
                (candidate.c.next_follow_up_at < today_start, "overdue"),
                else_="today",
            ).label("due_state"),
        )
        .select_from(candidate)
        .join(ContactFollowUp, candidate.c.follow_up_id == ContactFollowUp.id)
        .join(CustomerContact, candidate.c.contact_id == CustomerContact.id)
        .join(Customer, CustomerContact.customer_id == Customer.id)
        .join(
            ContactFunction,
            CustomerContact.function_id == ContactFunction.id,
        )
        .join(User, candidate.c.follow_up_user_id == User.id)
        .where(*conditions)
        .order_by(
            candidate.c.next_follow_up_at.asc(),
            candidate.c.follow_up_id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(statement)
    rows = [
        DueFollowUpRow(
            follow_up=follow_up,
            contact=contact,
            function_name=function_name,
            customer=customer,
            follow_up_user_nickname=nickname,
            follow_up_user_username=username,
            due_state=cast(Literal["today", "overdue"], row_due_state),
        )
        for (
            follow_up,
            contact,
            function_name,
            customer,
            nickname,
            username,
            row_due_state,
        ) in result.all()
    ]
    return rows, int(total or 0)


async def list_due_customers(
    session: AsyncSession,
    *,
    scope_filter: ColumnElement[bool],
    due_state: DueState,
    today_start: datetime,
    tomorrow_start: datetime,
    follow_up_user_id: int | None,
    page: int,
    page_size: int,
) -> tuple[list[DueCustomerRow], int]:
    """按客户聚合分页列出待跟进对接人数。"""
    candidate = _current_plan_candidates()
    conditions = _due_conditions(
        candidate,
        scope_filter=scope_filter,
        due_state=due_state,
        today_start=today_start,
        tomorrow_start=tomorrow_start,
        follow_up_user_id=follow_up_user_id,
    )
    earliest_next_follow_up_at = func.min(candidate.c.next_follow_up_at).label(
        "earliest_next_follow_up_at"
    )
    grouped_statement = (
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            func.count(func.distinct(candidate.c.contact_id)).label("due_count"),
            func.coalesce(
                func.sum(
                    case(
                        (candidate.c.next_follow_up_at < today_start, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("overdue_count"),
            earliest_next_follow_up_at,
        )
        .select_from(candidate)
        .join(
            CustomerContact,
            candidate.c.contact_id == CustomerContact.id,
        )
        .join(Customer, CustomerContact.customer_id == Customer.id)
        .where(*conditions)
        .group_by(Customer.id, Customer.name)
    )

    total_statement = select(func.count()).select_from(
        grouped_statement.subquery("due_customer_groups")
    )
    total = await session.scalar(total_statement)

    statement = (
        grouped_statement.order_by(
            earliest_next_follow_up_at.asc(),
            Customer.id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(statement)
    rows: list[DueCustomerRow] = []
    for (
        customer_id,
        customer_name,
        due_count,
        overdue_count,
        earliest,
    ) in result.all():
        rows.append(
            DueCustomerRow(
                customer_id=int(customer_id),
                customer_name=customer_name,
                due_count=int(due_count),
                overdue_count=int(overdue_count),
                earliest_next_follow_up_at=cast(datetime, earliest),
            )
        )
    return rows, int(total or 0)


async def count_due_states(
    session: AsyncSession,
    *,
    scope_filter: ColumnElement[bool],
    today_start: datetime,
    tomorrow_start: datetime,
    follow_up_user_id: int | None,
) -> tuple[int, int]:
    """统计待跟进对接人数，返回今日数与逾期数。"""
    candidate = _current_plan_candidates()
    conditions = _due_conditions(
        candidate,
        scope_filter=scope_filter,
        due_state="all",
        today_start=today_start,
        tomorrow_start=tomorrow_start,
        follow_up_user_id=follow_up_user_id,
    )
    statement = (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (candidate.c.next_follow_up_at >= today_start, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("today_count"),
            func.coalesce(
                func.sum(
                    case(
                        (candidate.c.next_follow_up_at < today_start, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("overdue_count"),
        )
        .select_from(candidate)
        .join(
            CustomerContact,
            candidate.c.contact_id == CustomerContact.id,
        )
        .join(Customer, CustomerContact.customer_id == Customer.id)
        .where(*conditions)
    )
    result = await session.execute(statement)
    today_count, overdue_count = result.one()
    return int(today_count or 0), int(overdue_count or 0)


__all__: tuple[str, ...] = (
    "DueCustomerRow",
    "DueFollowUpRow",
    "FollowUpRow",
    "add_attachment",
    "add_follow_up",
    "count_attachments",
    "count_due_states",
    "delete_attachment_row",
    "get_attachment",
    "get_follow_up",
    "get_follow_up_row",
    "get_method",
    "list_attachments",
    "list_due_customers",
    "list_due_follow_ups",
    "list_follow_ups",
    "list_methods",
    "lock_follow_up_for_update",
    "lock_follow_up_ids_referencing_method",
    "lock_method_for_update",
    "lock_method_shared",
)
