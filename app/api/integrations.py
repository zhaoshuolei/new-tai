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
from app.schemas.integration import FeishuIntegrationStatus, FeishuIntegrationUpdate
from app.services import feishu_integration as feishu_integration_service
from app.services import integration as integration_service

router = APIRouter(prefix="/integrations", tags=["集成"])


@router.get(
    "/feishu",
    response_model=SuccessEnvelope[FeishuIntegrationStatus],
    dependencies=[Depends(require_permission("integration_feishu:read"))],
)
async def get_feishu_integration(
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    status = await feishu_integration_service.get_feishu_integration_status(session)
    return success(status)


@router.put(
    "/feishu",
    response_model=SuccessEnvelope[FeishuIntegrationStatus],
    dependencies=[Depends(require_permission("integration_feishu:update"))],
)
async def update_feishu_integration(
    payload: FeishuIntegrationUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    status = await feishu_integration_service.save_feishu_integration(
        session,
        payload,
        context=context,
    )
    return success(status, message="updated")


@router.post(
    "/feishu/test",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("integration_feishu:update"))],
)
async def test_feishu_integration(
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await feishu_integration_service.test_feishu_connection(session)
    return success(message="ok")


@router.delete(
    "/feishu",
    response_model=SuccessEnvelope[FeishuIntegrationStatus],
    dependencies=[Depends(require_permission("integration_feishu:update"))],
)
async def delete_feishu_integration(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    status = await feishu_integration_service.delete_feishu_integration(
        session,
        context=context,
    )
    return success(status, message="deleted")


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
    "/integrations/feishu",
    action="integration_feishu:update",
    resource_type="integration",
)
register_audit_route(
    "DELETE",
    "/integrations/feishu",
    action="integration_feishu:update",
    resource_type="integration",
)
register_audit_route(
    "PUT",
    "/integrations/tianyancha",
    action="integration:update",
    resource_type="integration",
)


__all__: tuple[str, ...] = (
    "delete_feishu_integration",
    "get_feishu_integration",
    "get_tianyancha_integration",
    "router",
    "test_feishu_integration",
    "test_tianyancha_integration",
    "update_feishu_integration",
    "update_tianyancha_integration",
)
