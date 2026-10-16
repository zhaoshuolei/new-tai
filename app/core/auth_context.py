from dataclasses import dataclass

from app.models.role import DataScope


@dataclass(frozen=True)
class AuthContext:
    user_id: int
    role_id: int | None
    department_id: int | None
    data_scope: DataScope | None
    custom_department_ids: frozenset[int]
    include_children: bool
    permission_codes: frozenset[str]
    is_super_admin: bool


__all__: tuple[str, ...] = ("AuthContext",)
