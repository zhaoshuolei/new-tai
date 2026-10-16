"""客户管理接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.customer import CustomerSource
from app.schemas.customer import (
    CustomerCreate,
    CustomerImportRequest,
    CustomerOwnerUpdate,
    CustomerRead,
    CustomerSearchResponse,
    CustomerUpdate,
)
from app.services import customer as customer_service

router = APIRouter(prefix="/customers", tags=["客户"])

SEARCH_PAGE_SIZE_LIMIT = 50


@router.get(
    "",
    response_model=SuccessEnvelope[Page[CustomerRead]],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def list_customers(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    keyword: str | None = None,
    source: CustomerSource | None = None,
) -> dict[str, Any]:
    customers, total = await customer_service.list_customers(
        session,
        context=context,
        page=page,
        page_size=page_size,
        keyword=keyword,
        source=source,
    )
    return success(
        Page[CustomerRead](
            items=[CustomerRead.model_validate(customer) for customer in customers],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.post(
    "",
    response_model=SuccessEnvelope[CustomerRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("customer:create"))],
)
async def create_customer(
    payload: CustomerCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    customer = await customer_service.create_customer(
        session,
        payload,
        context=context,
    )
    return success(CustomerRead.model_validate(customer), message="created")


@router.get(
    "/search",
    response_model=SuccessEnvelope[CustomerSearchResponse],
    dependencies=[Depends(require_permission("customer:create"))],
)
async def search_candidates(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
    keyword: Annotated[str, Query(min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=SEARCH_PAGE_SIZE_LIMIT)] = 20,
) -> dict[str, Any]:
    result = await customer_service.search_candidates(
        session,
        keyword,
        page=page,
        page_size=page_size,
        context=context,
    )
    return success(result)


@router.post(
    "/import",
    response_model=SuccessEnvelope[CustomerRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("customer:create"))],
)
async def import_customer(
    payload: CustomerImportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    customer = await customer_service.import_from_tianyancha(
        session,
        payload.credit_code,
        payload.short_name,
        context=context,
    )
    return success(CustomerRead.model_validate(customer), message="created")


@router.get(
    "/{customer_id}",
    response_model=SuccessEnvelope[CustomerRead],
    dependencies=[Depends(require_permission("customer:read"))],
)
async def get_customer(
    customer_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    customer = await customer_service.get_customer(
        session,
        customer_id,
        context=context,
    )
    return success(CustomerRead.model_validate(customer))


@router.patch(
    "/{customer_id}",
    response_model=SuccessEnvelope[CustomerRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    customer = await customer_service.update_customer(
        session,
        customer_id,
        payload,
        context=context,
    )
    return success(CustomerRead.model_validate(customer))


@router.delete(
    "/{customer_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("customer:delete"))],
)
async def delete_customer(
    customer_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await customer_service.delete_customer(
        session,
        customer_id,
        context=context,
    )
    return success(message="deleted")


@router.patch(
    "/{customer_id}/owner",
    response_model=SuccessEnvelope[CustomerRead],
    dependencies=[Depends(require_permission("customer:update"))],
)
async def transfer_customer_owner(
    customer_id: int,
    payload: CustomerOwnerUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    customer = await customer_service.transfer_owner(
        session,
        customer_id,
        payload.owner_user_id,
        context=context,
    )
    return success(CustomerRead.model_validate(customer))


register_audit_route(
    "POST",
    "/customers",
    action="customer:create",
    resource_type="customer",
)
register_audit_route(
    "POST",
    "/customers/import",
    action="customer:create",
    resource_type="customer",
)
register_audit_route(
    "PATCH",
    "/customers/{customer_id}",
    action="customer:update",
    resource_type="customer",
)
register_audit_route(
    "DELETE",
    "/customers/{customer_id}",
    action="customer:delete",
    resource_type="customer",
)
register_audit_route(
    "PATCH",
    "/customers/{customer_id}/owner",
    action="customer:transfer",
    resource_type="customer",
)


__all__: tuple[str, ...] = (
    "create_customer",
    "delete_customer",
    "get_customer",
    "import_customer",
    "list_customers",
    "router",
    "search_candidates",
    "transfer_customer_owner",
    "update_customer",
)
