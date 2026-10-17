"""客户对接人与职能接口。"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.contact import ContactFunction, CustomerContact
from app.repositories import contact as contact_repository
from app.schemas.contact import (
    ContactCreate,
    ContactFunctionCreate,
    ContactFunctionRead,
    ContactFunctionUpdate,
    ContactRead,
    ContactUpdate,
)
from app.services import contact as contact_service

router = APIRouter(tags=["客户对接人"])


def _function_scope(
    function: ContactFunction,
) -> Literal["SHARED", "CUSTOM"]:
    return "SHARED" if function.customer_id is None else "CUSTOM"


def _function_read(
    function: ContactFunction,
    scope: Literal["SHARED", "CUSTOM"] | None = None,
) -> ContactFunctionRead:
    return ContactFunctionRead(
        id=function.id,
        name=function.name,
        customer_id=function.customer_id,
        scope=scope or _function_scope(function),
        created_at=function.created_at,
        updated_at=function.updated_at,
    )


def _contact_read(
    contact: CustomerContact,
    function: ContactFunction,
) -> ContactRead:
    return ContactRead(
        id=contact.id,
        customer_id=contact.customer_id,
        function_id=contact.function_id,
        function_name=function.name,
        function_scope=_function_scope(function),
        name=contact.name,
        position=contact.position,
        phone=contact.phone,
        email=contact.email,
        home_address=contact.home_address,
        remark=contact.remark,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
    )


async def _read_contact(
    session: AsyncSession,
    contact: CustomerContact,
) -> ContactRead:
    function = await contact_repository.get_function(session, contact.function_id)
    if function is None:
        raise RuntimeError("Contact function missing for persisted contact")
    return _contact_read(contact, function)


@router.get(
    "/contact-functions",
    response_model=SuccessEnvelope[list[ContactFunctionRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_shared_functions(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    functions = await contact_service.list_shared_functions(
        session,
        context=context,
        include_deleted=False,
    )
    return success([_function_read(function, "SHARED") for function in functions])


@router.get(
    "/contact-functions/recycle-bin",
    response_model=SuccessEnvelope[list[ContactFunctionRead]],
)
async def list_deleted_shared_functions(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    functions = await contact_service.list_shared_functions(
        session,
        context=context,
        include_deleted=True,
    )
    return success([_function_read(function, "SHARED") for function in functions])


@router.post(
    "/contact-functions",
    response_model=SuccessEnvelope[ContactFunctionRead],
)
async def create_shared_function(
    payload: ContactFunctionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    function = await contact_service.create_shared_function(
        session,
        payload.name,
        context=context,
    )
    return success(_function_read(function, "SHARED"), message="created")


@router.patch(
    "/contact-functions/{function_id}",
    response_model=SuccessEnvelope[ContactFunctionRead],
)
async def update_shared_function(
    function_id: int,
    payload: ContactFunctionUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    function = await contact_service.update_shared_function(
        session,
        function_id,
        payload.name,
        context=context,
    )
    return success(_function_read(function, "SHARED"))


@router.delete(
    "/contact-functions/{function_id}",
    response_model=SuccessEnvelope[None],
)
async def delete_shared_function(
    function_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    replacement_function_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    await contact_service.delete_shared_function(
        session,
        function_id,
        replacement_function_id=replacement_function_id,
        context=context,
    )
    return success(message="deleted")


@router.post(
    "/contact-functions/{function_id}/restore",
    response_model=SuccessEnvelope[ContactFunctionRead],
)
async def restore_shared_function(
    function_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    function = await contact_service.restore_shared_function(
        session,
        function_id,
        context=context,
    )
    return success(_function_read(function, "SHARED"))


@router.get(
    "/customers/{customer_id}/contact-functions",
    response_model=SuccessEnvelope[list[ContactFunctionRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_customer_functions(
    customer_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    functions = await contact_service.list_customer_functions(
        session,
        customer_id,
        context=context,
    )
    return success([_function_read(function, scope) for function, scope in functions])


@router.post(
    "/customers/{customer_id}/contact-functions",
    response_model=SuccessEnvelope[ContactFunctionRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def create_custom_function(
    customer_id: int,
    payload: ContactFunctionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    function = await contact_service.create_custom_function(
        session,
        customer_id,
        payload.name,
        context=context,
    )
    return success(_function_read(function, "CUSTOM"), message="created")


@router.get(
    "/customers/{customer_id}/contact-functions/recycle-bin",
    response_model=SuccessEnvelope[list[ContactFunctionRead]],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def list_customer_deleted_functions(
    customer_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    functions = await contact_service.list_customer_recycle_functions(
        session,
        customer_id,
        context=context,
    )
    return success([_function_read(function, "CUSTOM") for function in functions])


@router.patch(
    "/customers/{customer_id}/contact-functions/{function_id}",
    response_model=SuccessEnvelope[ContactFunctionRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def update_custom_function(
    customer_id: int,
    function_id: int,
    payload: ContactFunctionUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    function = await contact_service.update_custom_function(
        session,
        customer_id,
        function_id,
        payload.name,
        context=context,
    )
    return success(_function_read(function, "CUSTOM"))


@router.delete(
    "/customers/{customer_id}/contact-functions/{function_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def delete_custom_function(
    customer_id: int,
    function_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    replacement_function_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    await contact_service.delete_custom_function(
        session,
        customer_id,
        function_id,
        replacement_function_id=replacement_function_id,
        context=context,
    )
    return success(message="deleted")


@router.post(
    "/customers/{customer_id}/contact-functions/{function_id}/restore",
    response_model=SuccessEnvelope[ContactFunctionRead],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def restore_custom_function(
    customer_id: int,
    function_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    function = await contact_service.restore_custom_function(
        session,
        customer_id,
        function_id,
        context=context,
    )
    return success(_function_read(function, "CUSTOM"))


@router.get(
    "/customers/{customer_id}/contacts",
    response_model=SuccessEnvelope[Page[ContactRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_contacts(
    customer_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    contacts, total = await contact_service.list_contacts(
        session,
        customer_id,
        context=context,
        page=page,
        page_size=page_size,
    )
    return success(
        Page[ContactRead](
            items=[_contact_read(contact, function) for contact, function in contacts],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.post(
    "/customers/{customer_id}/contacts",
    response_model=SuccessEnvelope[ContactRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def create_contact(
    customer_id: int,
    payload: ContactCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    contact = await contact_service.create_contact(
        session,
        customer_id,
        payload,
        context=context,
    )
    return success(await _read_contact(session, contact), message="created")


@router.get(
    "/customers/{customer_id}/contacts/recycle-bin",
    response_model=SuccessEnvelope[Page[ContactRead]],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def list_deleted_contacts(
    customer_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    contacts, total = await contact_service.list_deleted_contacts(
        session,
        customer_id,
        context=context,
        page=page,
        page_size=page_size,
    )
    return success(
        Page[ContactRead](
            items=[_contact_read(contact, function) for contact, function in contacts],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.patch(
    "/customers/{customer_id}/contacts/{contact_id}",
    response_model=SuccessEnvelope[ContactRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def update_contact(
    customer_id: int,
    contact_id: int,
    payload: ContactUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    contact = await contact_service.update_contact(
        session,
        customer_id,
        contact_id,
        payload,
        context=context,
    )
    return success(await _read_contact(session, contact))


@router.delete(
    "/customers/{customer_id}/contacts/{contact_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def delete_contact(
    customer_id: int,
    contact_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await contact_service.delete_contact(
        session,
        customer_id,
        contact_id,
        context=context,
    )
    return success(message="deleted")


@router.post(
    "/customers/{customer_id}/contacts/{contact_id}/restore",
    response_model=SuccessEnvelope[ContactRead],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def restore_contact(
    customer_id: int,
    contact_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    contact = await contact_service.restore_contact(
        session,
        customer_id,
        contact_id,
        context=context,
    )
    return success(await _read_contact(session, contact))


register_audit_route(
    "POST",
    "/contact-functions",
    action="contact_function:create",
    resource_type="contact_function",
)
register_audit_route(
    "PATCH",
    "/contact-functions/{function_id}",
    action="contact_function:update",
    resource_type="contact_function",
)
register_audit_route(
    "DELETE",
    "/contact-functions/{function_id}",
    action="contact_function:delete",
    resource_type="contact_function",
)
register_audit_route(
    "POST",
    "/contact-functions/{function_id}/restore",
    action="contact_function:restore",
    resource_type="contact_function",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contact-functions",
    action="contact_function:create",
    resource_type="contact_function",
)
register_audit_route(
    "PATCH",
    "/customers/{customer_id}/contact-functions/{function_id}",
    action="contact_function:update",
    resource_type="contact_function",
)
register_audit_route(
    "DELETE",
    "/customers/{customer_id}/contact-functions/{function_id}",
    action="contact_function:delete",
    resource_type="contact_function",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contact-functions/{function_id}/restore",
    action="contact_function:restore",
    resource_type="contact_function",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contacts",
    action="contact:create",
    resource_type="customer_contact",
)
register_audit_route(
    "PATCH",
    "/customers/{customer_id}/contacts/{contact_id}",
    action="contact:update",
    resource_type="customer_contact",
)
register_audit_route(
    "DELETE",
    "/customers/{customer_id}/contacts/{contact_id}",
    action="contact:delete",
    resource_type="customer_contact",
)
register_audit_route(
    "POST",
    "/customers/{customer_id}/contacts/{contact_id}/restore",
    action="contact:restore",
    resource_type="customer_contact",
)


__all__: tuple[str, ...] = (
    "create_contact",
    "create_custom_function",
    "create_shared_function",
    "delete_contact",
    "delete_custom_function",
    "delete_shared_function",
    "list_contacts",
    "list_customer_deleted_functions",
    "list_customer_functions",
    "list_deleted_contacts",
    "list_deleted_shared_functions",
    "list_shared_functions",
    "restore_contact",
    "restore_custom_function",
    "restore_shared_function",
    "router",
    "update_contact",
    "update_custom_function",
    "update_shared_function",
)
