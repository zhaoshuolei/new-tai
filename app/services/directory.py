"""Colleague visible directory services."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import Page
from app.repositories import department as department_repository
from app.repositories import user_profile as user_profile_repository
from app.repositories.user_profile import DirectoryRow
from app.schemas.directory import DirectoryDepartmentRead, DirectoryEntryRead


def display_name(nickname: str | None, username: str | None, user_id: int) -> str:
    return nickname or username or f"用户{user_id}"


def clip_scalar(
    value: str | None,
    *,
    key: str,
    hidden: list[str],
) -> str | None:
    return None if key in hidden else value


def clip_list[T](
    value: list[T],
    *,
    key: str,
    hidden: list[str],
) -> list[T] | None:
    return None if key in hidden else value


def build_directory_entry(row: DirectoryRow) -> DirectoryEntryRead:
    hidden = row.hidden_public_fields or []
    return DirectoryEntryRead(
        id=row.id,
        display_name=display_name(row.nickname, row.username, row.id),
        nickname=row.nickname,
        avatar=clip_scalar(row.avatar, key="avatar", hidden=hidden),
        signature=clip_scalar(row.signature, key="signature", hidden=hidden),
        department_name=row.department_name,
    )


async def list_directory(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    keyword: str | None,
    department_id: int | None,
) -> Page[DirectoryEntryRead]:
    rows, total = await user_profile_repository.list_directory_entries(
        session,
        page=page,
        page_size=page_size,
        keyword=keyword,
        department_id=department_id,
    )
    return Page[DirectoryEntryRead](
        items=[build_directory_entry(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


async def list_directory_departments(
    session: AsyncSession,
) -> list[DirectoryDepartmentRead]:
    departments = await department_repository.list_all(
        session,
        include_disabled=False,
    )
    return [
        DirectoryDepartmentRead(
            id=department.id,
            name=department.name,
            parent_id=department.parent_id,
            sort_order=department.sort_order,
        )
        for department in departments
        if not department.is_system
    ]


__all__: tuple[str, ...] = (
    "build_directory_entry",
    "clip_list",
    "clip_scalar",
    "display_name",
    "list_directory",
    "list_directory_departments",
)
