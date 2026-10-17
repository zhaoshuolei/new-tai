"""API v1 路由聚合。"""

from fastapi import APIRouter

from app.api.contacts import router as contacts_router
from app.api.customers import router as customers_router
from app.api.departments import router as departments_router
from app.api.directory import router as directory_router
from app.api.integrations import router as integrations_router
from app.api.logs import router as logs_router
from app.api.menus import me_router
from app.api.menus import router as menus_router
from app.api.monitor import exceptions_router as exceptions_monitor_router
from app.api.monitor import router as monitor_router
from app.api.profile import router as profile_router
from app.api.roles import router as roles_router
from app.api.system_settings import router as system_settings_router
from app.api.users import router as users_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(contacts_router)
api_v1_router.include_router(customers_router)
api_v1_router.include_router(departments_router)
api_v1_router.include_router(directory_router)
api_v1_router.include_router(exceptions_monitor_router)
api_v1_router.include_router(integrations_router)
api_v1_router.include_router(logs_router)
api_v1_router.include_router(menus_router)
api_v1_router.include_router(me_router)
api_v1_router.include_router(monitor_router)
api_v1_router.include_router(profile_router)
api_v1_router.include_router(roles_router)
api_v1_router.include_router(system_settings_router)
api_v1_router.include_router(users_router)


__all__: tuple[str, ...] = ("api_v1_router",)
