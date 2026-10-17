"""系统基础设置数据访问。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting

_GLOBAL_KEY = "global"


async def get_global(session: AsyncSession) -> SystemSetting | None:
    statement = select(SystemSetting).where(SystemSetting.setting_key == _GLOBAL_KEY)
    return await session.scalar(statement)


async def _get_global_for_update(session: AsyncSession) -> SystemSetting | None:
    statement = (
        select(SystemSetting)
        .where(SystemSetting.setting_key == _GLOBAL_KEY)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def upsert_global(
    session: AsyncSession,
    values: dict[str, object],
    *,
    updated_by: int | None,
) -> SystemSetting:
    """写入全局设置；仅 flush，事务边界由服务层控制。"""
    setting = await get_global(session)
    if setting is None:
        try:
            async with session.begin_nested():
                setting = SystemSetting(
                    setting_key=_GLOBAL_KEY,
                    updated_by=updated_by,
                    **values,
                )
                session.add(setting)
                await session.flush()
        except IntegrityError:
            setting = await _get_global_for_update(session)
            if setting is None:
                raise

    _apply_values(setting, values)
    setting.updated_by = updated_by
    await session.flush()
    await session.refresh(setting)
    return setting


def _apply_values(setting: SystemSetting, values: dict[str, object]) -> None:
    for field_name, value in values.items():
        setattr(setting, field_name, value)


__all__: tuple[str, ...] = ("get_global", "upsert_global")
