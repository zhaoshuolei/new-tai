"""Colleague directory response models."""

from pydantic import BaseModel

from app.models.user import UserStatus
from app.schemas.profile import PersonalLink


class DirectoryEntryRead(BaseModel):
    id: int
    display_name: str
    nickname: str | None
    avatar: str | None
    signature: str | None
    department_name: str | None


class DirectoryProfileRead(BaseModel):
    id: int
    display_name: str
    nickname: str | None
    department_name: str | None
    status: UserStatus
    avatar: str | None
    signature: str | None
    bio: str | None
    work_phone: str | None
    extension: str | None
    work_email: str | None
    office_location: str | None
    seat: str | None
    expertise_tags: list[str] | None
    personal_links: list[PersonalLink] | None


class DirectoryDepartmentRead(BaseModel):
    id: int
    name: str
    parent_id: int | None
    sort_order: int


__all__: tuple[str, ...] = (
    "DirectoryDepartmentRead",
    "DirectoryEntryRead",
    "DirectoryProfileRead",
)
