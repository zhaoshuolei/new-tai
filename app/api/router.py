"""API v1 路由聚合。"""

from fastapi import APIRouter

from app.api.departments import router as departments_router
from app.api.menus import router as menus_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(departments_router)
api_v1_router.include_router(menus_router)


__all__: tuple[str, ...] = ("api_v1_router",)
