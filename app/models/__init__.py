"""领域模型包，模型模块导入后向 Base.metadata 注册表定义。"""

from app.models.department import Department, DepartmentStatus
from app.models.menu import Menu, MenuStatus, MenuType
from app.models.role import (
    DataScope,
    Role,
    RoleMenu,
    RoleStatus,
)
from app.models.role_department import RoleDepartment
from app.models.user import User, UserStatus

__all__: tuple[str, ...] = (
    "DataScope",
    "Department",
    "DepartmentStatus",
    "Menu",
    "MenuStatus",
    "MenuType",
    "Role",
    "RoleDepartment",
    "RoleMenu",
    "RoleStatus",
    "User",
    "UserStatus",
)
