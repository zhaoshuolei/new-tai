"""Colleague directory API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.response import (
    MAX_PAGE_SIZE,
    Page,
    SuccessEnvelope,
    success,
)
from app.db.session import get_session
from app.schemas.directory import (
    DirectoryDepartmentRead,
    DirectoryEntryRead,
    DirectoryProfileRead,
)
from app.services import directory as directory_service

router = APIRouter(prefix="/directory", tags=["通讯录"])


@router.get(
    "",
    response_model=SuccessEnvelope[Page[DirectoryEntryRead]],
)
async def list_directory(
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    keyword: str | None = None,
    department_id: int | None = None,
) -> dict[str, Any]:
    entries = await directory_service.list_directory(
        session,
        page=page,
        page_size=page_size,
        keyword=keyword,
        department_id=department_id,
    )
    return success(entries)


@router.get(
    "/departments",
    response_model=SuccessEnvelope[list[DirectoryDepartmentRead]],
)
async def list_directory_departments(
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    departments = await directory_service.list_directory_departments(session)
    return success(departments)


@router.get(
    "/{user_id}",
    response_model=SuccessEnvelope[DirectoryProfileRead],
)
async def get_directory_profile(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    profile = await directory_service.get_directory_profile(
        session,
        user_id=user_id,
    )
    return success(profile)


__all__: tuple[str, ...] = (
    "get_directory_profile",
    "list_directory",
    "list_directory_departments",
    "router",
)
