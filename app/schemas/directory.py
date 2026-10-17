"""Colleague directory response models."""

from pydantic import BaseModel


class DirectoryEntryRead(BaseModel):
    id: int
    display_name: str
    nickname: str | None
    avatar: str | None
    signature: str | None
    department_name: str | None


class DirectoryDepartmentRead(BaseModel):
    id: int
    name: str
    parent_id: int | None
    sort_order: int


__all__: tuple[str, ...] = ("DirectoryDepartmentRead", "DirectoryEntryRead")
