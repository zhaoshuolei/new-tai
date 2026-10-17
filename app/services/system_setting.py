"""系统设置业务逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.best_effort import run_best_effort
from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.models.system_setting import SystemSetting
from app.repositories.system_setting import get_global, upsert_global
from app.schemas.system_setting import (
    PublicSystemSettingRead,
    SystemSettingRead,
    SystemSettingUpdate,
)
from app.services.audit import audited
from app.services.system_setting_asset import remove_system_asset


async def get_public_setting(session: AsyncSession) -> PublicSystemSettingRead:
    setting = await get_global(session)
    if setting is None:
        return PublicSystemSettingRead(
            name=settings.app_name,
            short_name=None,
            site_url=None,
            logo_url=None,
            favicon_url=None,
            description=None,
            copyright=None,
            icp_number=None,
        )
    return PublicSystemSettingRead.model_validate(setting)


async def get_admin_setting(session: AsyncSession) -> SystemSettingRead:
    setting = await get_global(session)
    if setting is None:
        return SystemSettingRead(
            name=settings.app_name,
            short_name=None,
            site_url=None,
            logo_url=None,
            favicon_url=None,
            description=None,
            copyright=None,
            icp_number=None,
            updated_at=None,
            updated_by=None,
        )
    return SystemSettingRead.model_validate(setting)


@audited(
    action="system_setting:update",
    resource_type="system_setting",
    resource_id_attr="id",
)
async def update_setting(
    session: AsyncSession,
    payload: SystemSettingUpdate,
    *,
    context: AuthContext | None = None,
) -> SystemSetting:
    values = payload.model_dump()
    raw_name = values.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Website name must not be empty",
            http_status=422,
        )
    values["name"] = name
    previous = await get_global(session)
    previous_urls = (
        previous.logo_url if previous is not None else None,
        previous.favicon_url if previous is not None else None,
    )

    setting = await upsert_global(
        session,
        values,
        updated_by=context.user_id if context is not None else None,
    )
    await session.commit()
    await session.refresh(setting)

    current_urls = {setting.logo_url, setting.favicon_url}
    for previous_url in previous_urls:
        if previous_url is None or previous_url in current_urls:
            continue

        async def remove_previous(url: str = previous_url) -> None:
            await remove_system_asset(url)

        await run_best_effort(
            remove_previous,
            description="remove superseded system setting asset",
        )
    return setting


__all__: tuple[str, ...] = (
    "get_admin_setting",
    "get_public_setting",
    "update_setting",
)
