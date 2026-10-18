"""客户跟进方式、跟进记录、待跟进清单与附件接口。"""

from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.follow_up import (
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)
from app.repositories import follow_up as follow_up_repository
from app.repositories.follow_up import DueCustomerRow, DueFollowUpRow, FollowUpRow
from app.schemas.follow_up import (
    DueCustomerRead,
    DueFollowUpRead,
    DueSummaryRead,
    FollowUpAttachmentRead,
    FollowUpCreate,
    FollowUpMethodCreate,
    FollowUpMethodRead,
    FollowUpMethodUpdate,
    FollowUpRead,
    FollowUpUpdate,
)
from app.services import follow_up as follow_up_service
from app.services import follow_up_attachment as attachment_service
from app.services.directory import display_name

router = APIRouter(tags=["客户跟进"])


def _method_read(method: ContactFollowUpMethod) -> FollowUpMethodRead:
    return FollowUpMethodRead.model_validate(method)


def _attachment_read(
    attachment: ContactFollowUpAttachment,
) -> FollowUpAttachmentRead:
    return FollowUpAttachmentRead(
        id=attachment.id,
        original_name=attachment.original_name,
        content_type=attachment.content_type,
        size_bytes=attachment.size_bytes,
        created_at=attachment.created_at,
    )


def _follow_up_read(
    row: FollowUpRow,
    customer_id: int,
    attachments: list[ContactFollowUpAttachment],
) -> FollowUpRead:
    follow_up = row.follow_up
    return FollowUpRead(
        id=follow_up.id,
        customer_id=customer_id,
        contact_id=follow_up.contact_id,
        contact_name=row.contact_name,
        method_id=follow_up.method_id,
        method_name=row.method_name,
        follow_up_user_id=follow_up.follow_up_user_id,
        follow_up_user_name=display_name(
            row.follow_up_user_nickname,
            row.follow_up_user_username,
            follow_up.follow_up_user_id,
        ),
        created_by=follow_up.created_by,
        created_by_name=display_name(
            row.created_by_nickname,
            row.created_by_username,
            follow_up.created_by,
        ),
        follow_up_at=follow_up.follow_up_at,
        content=follow_up.content,
        next_follow_up_at=follow_up.next_follow_up_at,
        attachments=[_attachment_read(attachment) for attachment in attachments],
        created_at=follow_up.created_at,
        updated_at=follow_up.updated_at,
    )


async def _follow_ups_read(
    session: AsyncSession,
    customer_id: int,
    rows: list[FollowUpRow],
) -> list[FollowUpRead]:
    attachments_by_follow_up = await follow_up_repository.list_attachments(
        session,
        [row.follow_up.id for row in rows],
    )
    return [
        _follow_up_read(
            row,
            customer_id,
            attachments_by_follow_up.get(row.follow_up.id, []),
        )
        for row in rows
    ]


async def _follow_up_only_read(
    session: AsyncSession,
    customer_id: int,
    row: FollowUpRow,
) -> FollowUpRead:
    return (await _follow_ups_read(session, customer_id, [row]))[0]


def _due_follow_up_read(row: DueFollowUpRow) -> DueFollowUpRead:
    next_follow_up_at = row.follow_up.next_follow_up_at
    assert next_follow_up_at is not None
    return DueFollowUpRead(
        follow_up_id=row.follow_up.id,
        due_state=row.due_state,
        next_follow_up_at=next_follow_up_at,
        follow_up_at=row.follow_up.follow_up_at,
        customer_id=row.customer.id,
        customer_name=row.customer.name,
        contact_id=row.contact.id,
        contact_name=row.contact.name,
        function_name=row.function_name,
        follow_up_user_id=row.follow_up.follow_up_user_id,
        follow_up_user_name=display_name(
            row.follow_up_user_nickname,
            row.follow_up_user_username,
            row.follow_up.follow_up_user_id,
        ),
    )


def _due_customer_read(row: DueCustomerRow) -> DueCustomerRead:
    return DueCustomerRead(
        customer_id=row.customer_id,
        customer_name=row.customer_name,
        due_count=row.due_count,
        overdue_count=row.overdue_count,
        earliest_next_follow_up_at=row.earliest_next_follow_up_at,
    )


# 跟进方式。


@router.get(
    "/follow-up-methods",
    response_model=SuccessEnvelope[list[FollowUpMethodRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_methods(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    methods = await follow_up_service.list_methods(session, context=context)
    return success([_method_read(method) for method in methods])


@router.get(
    "/follow-up-methods/recycle-bin",
    response_model=SuccessEnvelope[list[FollowUpMethodRead]],
)
async def list_deleted_methods(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    methods = await follow_up_service.list_deleted_methods(
        session,
        context=context,
    )
    return success([_method_read(method) for method in methods])


@router.post(
    "/follow-up-methods",
    response_model=SuccessEnvelope[FollowUpMethodRead],
)
async def create_method(
    payload: FollowUpMethodCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    method = await follow_up_service.create_method(
        session,
        payload.name,
        context=context,
    )
    return success(_method_read(method), message="created")


@router.patch(
    "/follow-up-methods/{method_id}",
    response_model=SuccessEnvelope[FollowUpMethodRead],
)
async def update_method(
    method_id: int,
    payload: FollowUpMethodUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    method = await follow_up_service.update_method(
        session,
        method_id,
        payload.name,
        context=context,
    )
    return success(_method_read(method))


@router.delete(
    "/follow-up-methods/{method_id}",
    response_model=SuccessEnvelope[None],
)
async def delete_method(
    method_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await follow_up_service.delete_method(session, method_id, context=context)
    return success(message="deleted")


@router.post(
    "/follow-up-methods/{method_id}/restore",
    response_model=SuccessEnvelope[FollowUpMethodRead],
)
async def restore_method(
    method_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    method = await follow_up_service.restore_method(
        session,
        method_id,
        context=context,
    )
    return success(_method_read(method))


# 跟进记录。


@router.get(
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups",
    response_model=SuccessEnvelope[Page[FollowUpRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_follow_ups(
    customer_id: int,
    contact_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    rows, total = await follow_up_service.list_follow_ups(
        session,
        customer_id,
        contact_id,
        context=context,
        page=page,
        page_size=page_size,
    )
    return success(
        Page[FollowUpRead](
            items=await _follow_ups_read(session, customer_id, rows),
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.post(
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups",
    response_model=SuccessEnvelope[FollowUpRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def create_follow_up(
    customer_id: int,
    contact_id: int,
    payload: FollowUpCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    row = await follow_up_service.create_follow_up(
        session,
        customer_id,
        contact_id,
        payload,
        context=context,
    )
    return success(
        await _follow_up_only_read(session, customer_id, row),
        message="created",
    )


@router.get(
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/recycle-bin",
    response_model=SuccessEnvelope[Page[FollowUpRead]],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def list_deleted_follow_ups(
    customer_id: int,
    contact_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    rows, total = await follow_up_service.list_deleted_follow_ups(
        session,
        customer_id,
        contact_id,
        context=context,
        page=page,
        page_size=page_size,
    )
    return success(
        Page[FollowUpRead](
            items=await _follow_ups_read(session, customer_id, rows),
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.patch(
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}",
    response_model=SuccessEnvelope[FollowUpRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def update_follow_up(
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    payload: FollowUpUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    row = await follow_up_service.update_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        payload,
        context=context,
    )
    return success(await _follow_up_only_read(session, customer_id, row))


@router.delete(
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def delete_follow_up(
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await follow_up_service.delete_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        context=context,
    )
    return success(message="deleted")


@router.post(
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/restore",
    response_model=SuccessEnvelope[FollowUpRead],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def restore_follow_up(
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    row = await follow_up_service.restore_follow_up(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        context=context,
    )
    return success(await _follow_up_only_read(session, customer_id, row))


# 待跟进清单。


@router.get(
    "/follow-ups/due",
    response_model=SuccessEnvelope[Page[DueFollowUpRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_due_follow_ups(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    due: Annotated[Literal["today", "overdue", "all"], Query()] = "all",
    follow_up_user_id: Annotated[int | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    rows, total = await follow_up_service.list_due_follow_ups(
        session,
        context=context,
        due=due,
        follow_up_user_id=follow_up_user_id,
        page=page,
        page_size=page_size,
    )
    return success(
        Page[DueFollowUpRead](
            items=[_due_follow_up_read(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/follow-ups/due/customers",
    response_model=SuccessEnvelope[Page[DueCustomerRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_due_customers(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    due: Annotated[Literal["today", "overdue", "all"], Query()] = "all",
    follow_up_user_id: Annotated[int | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    rows, total = await follow_up_service.list_due_customers(
        session,
        context=context,
        due=due,
        follow_up_user_id=follow_up_user_id,
        page=page,
        page_size=page_size,
    )
    return success(
        Page[DueCustomerRead](
            items=[_due_customer_read(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/follow-ups/due/summary",
    response_model=SuccessEnvelope[DueSummaryRead],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def due_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    follow_up_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    today_count, overdue_count = await follow_up_service.due_summary(
        session,
        context=context,
        follow_up_user_id=follow_up_user_id,
    )
    return success(DueSummaryRead(today=today_count, overdue=overdue_count))


# 跟进附件。


@router.post(
    "/customers/{customer_id}/contacts/{contact_id}"
    "/follow-ups/{follow_up_id}/attachments",
    response_model=SuccessEnvelope[FollowUpAttachmentRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def upload_attachment(
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    encoded_name = request.headers.get("x-file-name")
    try:
        original_name = unquote(encoded_name, errors="strict") if encoded_name else None
    except TypeError, ValueError, UnicodeError:
        original_name = None
    attachment = await attachment_service.upload_attachment(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        content_type=request.headers.get("content-type"),
        original_name=original_name,
        chunks=request.stream(),
        context=context,
    )
    return success(_attachment_read(attachment), message="created")


@router.get(
    "/customers/{customer_id}/contacts/{contact_id}"
    "/follow-ups/{follow_up_id}/attachments/{attachment_id}/download",
    response_class=FileResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
async def download_attachment(
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    attachment_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> FileResponse:
    path, attachment = await attachment_service.get_attachment_for_download(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        attachment_id,
        context=context,
    )
    return FileResponse(
        path,
        media_type=attachment.content_type,
        filename=attachment.original_name
        or f"attachment-{attachment.id}{Path(attachment.storage_path).suffix}",
        headers={"Cache-Control": "no-store"},
    )


@router.delete(
    "/customers/{customer_id}/contacts/{contact_id}"
    "/follow-ups/{follow_up_id}/attachments/{attachment_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def delete_attachment(
    customer_id: int,
    contact_id: int,
    follow_up_id: int,
    attachment_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await attachment_service.delete_attachment(
        session,
        customer_id,
        contact_id,
        follow_up_id,
        attachment_id,
        context=context,
    )
    return success(message="deleted")


register_audit_route(
    "POST",
    "/follow-up-methods",
    action="follow_up_method:create",
    resource_type="contact_follow_up_method",
)
register_audit_route(
    "PATCH",
    "/follow-up-methods/{method_id}",
    action="follow_up_method:update",
    resource_type="contact_follow_up_method",
    resource_id_param="method_id",
)
register_audit_route(
    "DELETE",
    "/follow-up-methods/{method_id}",
    action="follow_up_method:delete",
    resource_type="contact_follow_up_method",
    resource_id_param="method_id",
)
register_audit_route(
    "POST",
    "/follow-up-methods/{method_id}/restore",
    action="follow_up_method:restore",
    resource_type="contact_follow_up_method",
    resource_id_param="method_id",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups",
    action="contact_follow_up:create",
    resource_type="contact_follow_up",
)
register_audit_route(
    "PATCH",
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}",
    action="contact_follow_up:update",
    resource_type="contact_follow_up",
    resource_id_param="follow_up_id",
)
register_audit_route(
    "DELETE",
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}",
    action="contact_follow_up:delete",
    resource_type="contact_follow_up",
    resource_id_param="follow_up_id",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/restore",
    action="contact_follow_up:restore",
    resource_type="contact_follow_up",
    resource_id_param="follow_up_id",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contacts/{contact_id}"
    "/follow-ups/{follow_up_id}/attachments",
    action="contact_follow_up:attachment_upload",
    resource_type="contact_follow_up_attachment",
)
register_audit_route(
    "DELETE",
    "/customers/{customer_id}/contacts/{contact_id}"
    "/follow-ups/{follow_up_id}/attachments/{attachment_id}",
    action="contact_follow_up:attachment_delete",
    resource_type="contact_follow_up_attachment",
    resource_id_param="attachment_id",
)


__all__: tuple[str, ...] = (
    "create_follow_up",
    "create_method",
    "delete_attachment",
    "delete_follow_up",
    "delete_method",
    "download_attachment",
    "due_summary",
    "list_deleted_follow_ups",
    "list_deleted_methods",
    "list_due_customers",
    "list_due_follow_ups",
    "list_follow_ups",
    "list_methods",
    "restore_follow_up",
    "restore_method",
    "router",
    "update_follow_up",
    "update_method",
    "upload_attachment",
)
