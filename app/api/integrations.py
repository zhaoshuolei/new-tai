"""第三方集成配置接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.customer import IntegrationStatus, IntegrationTokenUpdate
from app.services import integration as integration_service

router = APIRouter(prefix="/integrations", tags=["集成"])


@router.get(
    "/tianyancha",
    response_model=SuccessEnvelope[IntegrationStatus],
    dependencies=[Depends(require_permission("integration:read"))],
)
async def get_tianyancha_integration(
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    status = await integration_service.get_tianyancha_status(session)
    return success(IntegrationStatus.model_validate(status))


@router.put(
    "/tianyancha",
    response_model=SuccessEnvelope[IntegrationStatus],
    dependencies=[Depends(require_permission("integration:update"))],
)
async def update_tianyancha_integration(
    payload: IntegrationTokenUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await integration_service.save_tianyancha_token(
        session,
        payload.api_key,
        context=context,
    )
    status = await integration_service.get_tianyancha_status(session)
    return success(IntegrationStatus.model_validate(status), message="updated")


@router.post(
    "/tianyancha/test",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("integration:update"))],
)
async def test_tianyancha_integration(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await integration_service.test_tianyancha_connection(session, context=context)
    return success(message="ok")


register_audit_route(
    "PUT",
    "/integrations/tianyancha",
    action="integration:update",
    resource_type="integration",
)


__all__: tuple[str, ...] = (
    "get_tianyancha_integration",
    "router",
    "test_tianyancha_integration",
    "update_tianyancha_integration",
)
