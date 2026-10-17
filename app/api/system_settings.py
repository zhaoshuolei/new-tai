"""系统设置接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.system_setting import (
    PublicSystemSettingRead,
    SystemSettingRead,
    SystemSettingUpdate,
)
from app.services import system_setting as system_setting_service
from app.services.system_setting_asset import (
    SystemAssetType,
    upload_system_asset,
)

router = APIRouter(prefix="/system-settings", tags=["系统设置"])


@router.get(
    "/public",
    response_model=SuccessEnvelope[PublicSystemSettingRead],
)
async def get_public_system_setting(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    setting = await system_setting_service.get_public_setting(session)
    return success(setting)


@router.get(
    "",
    response_model=SuccessEnvelope[SystemSettingRead],
    dependencies=[Depends(require_permission("system_setting:read"))],
)
async def get_system_setting(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    setting = await system_setting_service.get_admin_setting(session)
    return success(setting)


@router.put(
    "",
    response_model=SuccessEnvelope[SystemSettingRead],
    dependencies=[Depends(require_permission("system_setting:update"))],
)
async def update_system_setting(
    payload: SystemSettingUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    setting = await system_setting_service.update_setting(
        session,
        payload,
        context=context,
    )
    return success(SystemSettingRead.model_validate(setting), message="updated")


@router.post(
    "/assets/{asset_type}",
    response_model=SuccessEnvelope[dict[str, str]],
    dependencies=[Depends(require_permission("system_setting:update"))],
)
async def upload_system_setting_asset(
    asset_type: SystemAssetType,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    url = await upload_system_asset(
        asset_type=asset_type,
        content_type=request.headers.get("content-type"),
        chunks=request.stream(),
        context=context,
    )
    return success({"url": url})


register_audit_route(
    "PUT",
    "/system-settings",
    action="system_setting:update",
    resource_type="system_setting",
)
register_audit_route(
    "POST",
    "/system-settings/assets/{asset_type}",
    action="system_setting:asset_upload",
    resource_type="system_setting",
)


__all__: tuple[str, ...] = (
    "get_public_system_setting",
    "get_system_setting",
    "router",
    "update_system_setting",
    "upload_system_setting_asset",
)
