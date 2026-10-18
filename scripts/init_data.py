"""幂等的初始化数据脚本。"""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.password_policy import validate_password_policy
from app.core.security import hash_password
from app.db.session import session_factory
from app.models.contact import ContactFunction, ContactStatus
from app.models.department import Department, DepartmentStatus
from app.models.menu import Menu, MenuStatus, MenuType
from app.models.role import DataScope, Role, RoleMenu, RoleStatus
from app.models.system_setting import SystemSetting
from app.models.user import User, UserStatus
from app.schemas.menu import MenuCreate
from app.services.auth import normalize_username
from app.services.contact import serialize_contact_function_name_change

logger = logging.getLogger(__name__)

_PENDING_ASSIGNMENT_DEPARTMENT_NAME = "待分配"
_SUPER_ADMIN_ROLE_CODE = "super_admin"
_DEFAULT_ROLE_CODE = "default"
_DIRECTORY_MENU_NAME = "通讯录"
_PROFILE_MENU_NAME = "个人资料"
_SHARED_CONTACT_FUNCTION_NAMES = ("商务", "运营", "研发", "产品")


@dataclass(frozen=True)
class _MenuSeed:
    key: str
    parent_key: str | None
    menu_type: MenuType
    name: str
    path: str | None
    permission_code: str | None = None
    sort_order: int = 0


_MENU_SEEDS = (
    _MenuSeed(
        key="directory",
        parent_key=None,
        menu_type=MenuType.MENU,
        name=_DIRECTORY_MENU_NAME,
        path="/directory",
        sort_order=5,
    ),
    _MenuSeed(
        key="profile",
        parent_key=None,
        menu_type=MenuType.MENU,
        name=_PROFILE_MENU_NAME,
        path="/profile",
        sort_order=10,
    ),
    _MenuSeed(
        key="system",
        parent_key=None,
        menu_type=MenuType.DIR,
        name="系统管理",
        path="/system",
        sort_order=20,
    ),
    _MenuSeed(
        key="user",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="用户管理",
        path="/system/users",
        permission_code="user:read",
        sort_order=10,
    ),
    _MenuSeed(
        key="user-create",
        parent_key="user",
        menu_type=MenuType.BUTTON,
        name="新增用户",
        path=None,
        permission_code="user:create",
        sort_order=10,
    ),
    _MenuSeed(
        key="user-update",
        parent_key="user",
        menu_type=MenuType.BUTTON,
        name="编辑用户",
        path=None,
        permission_code="user:update",
        sort_order=20,
    ),
    _MenuSeed(
        key="user-delete",
        parent_key="user",
        menu_type=MenuType.BUTTON,
        name="删除用户",
        path=None,
        permission_code="user:delete",
        sort_order=30,
    ),
    _MenuSeed(
        key="user-assign",
        parent_key="user",
        menu_type=MenuType.BUTTON,
        name="分配用户角色与部门",
        path=None,
        permission_code="user:assign",
        sort_order=40,
    ),
    _MenuSeed(
        key="role",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="角色管理",
        path="/system/roles",
        permission_code="role:read",
        sort_order=20,
    ),
    _MenuSeed(
        key="role-create",
        parent_key="role",
        menu_type=MenuType.BUTTON,
        name="新增角色",
        path=None,
        permission_code="role:create",
        sort_order=10,
    ),
    _MenuSeed(
        key="role-update",
        parent_key="role",
        menu_type=MenuType.BUTTON,
        name="编辑角色",
        path=None,
        permission_code="role:update",
        sort_order=20,
    ),
    _MenuSeed(
        key="role-delete",
        parent_key="role",
        menu_type=MenuType.BUTTON,
        name="删除角色",
        path=None,
        permission_code="role:delete",
        sort_order=30,
    ),
    _MenuSeed(
        key="role-assign",
        parent_key="role",
        menu_type=MenuType.BUTTON,
        name="分配角色权限",
        path=None,
        permission_code="role:assign",
        sort_order=40,
    ),
    _MenuSeed(
        key="menu",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="菜单管理",
        path="/system/menus",
        permission_code="menu:read",
        sort_order=30,
    ),
    _MenuSeed(
        key="menu-create",
        parent_key="menu",
        menu_type=MenuType.BUTTON,
        name="新增菜单",
        path=None,
        permission_code="menu:create",
        sort_order=10,
    ),
    _MenuSeed(
        key="menu-update",
        parent_key="menu",
        menu_type=MenuType.BUTTON,
        name="编辑菜单",
        path=None,
        permission_code="menu:update",
        sort_order=20,
    ),
    _MenuSeed(
        key="menu-delete",
        parent_key="menu",
        menu_type=MenuType.BUTTON,
        name="删除菜单",
        path=None,
        permission_code="menu:delete",
        sort_order=30,
    ),
    _MenuSeed(
        key="dept",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="部门管理",
        path="/system/departments",
        permission_code="dept:read",
        sort_order=40,
    ),
    _MenuSeed(
        key="dept-create",
        parent_key="dept",
        menu_type=MenuType.BUTTON,
        name="新增部门",
        path=None,
        permission_code="dept:create",
        sort_order=10,
    ),
    _MenuSeed(
        key="dept-update",
        parent_key="dept",
        menu_type=MenuType.BUTTON,
        name="编辑部门",
        path=None,
        permission_code="dept:update",
        sort_order=20,
    ),
    _MenuSeed(
        key="dept-delete",
        parent_key="dept",
        menu_type=MenuType.BUTTON,
        name="删除部门",
        path=None,
        permission_code="dept:delete",
        sort_order=30,
    ),
    _MenuSeed(
        key="logs",
        parent_key="system",
        menu_type=MenuType.DIR,
        name="日志管理",
        path="/system/logs",
        sort_order=50,
    ),
    _MenuSeed(
        key="logs-operations",
        parent_key="logs",
        menu_type=MenuType.MENU,
        name="操作日志",
        path="/system/logs/operations",
        permission_code="audit:read",
        sort_order=10,
    ),
    _MenuSeed(
        key="logs-logins",
        parent_key="logs",
        menu_type=MenuType.MENU,
        name="登录日志",
        path="/system/logs/logins",
        permission_code="login_log:read",
        sort_order=20,
    ),
    _MenuSeed(
        key="monitor",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="系统监控",
        path="/system/monitor",
        permission_code="monitor:read",
        sort_order=60,
    ),
    _MenuSeed(
        key="exceptions",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="异常监控",
        path="/system/monitor/exceptions",
        permission_code="exception:read",
        sort_order=70,
    ),
    _MenuSeed(
        key="exceptions-ack",
        parent_key="exceptions",
        menu_type=MenuType.BUTTON,
        name="标记处理",
        path=None,
        permission_code="exception:update",
        sort_order=10,
    ),
    _MenuSeed(
        key="integration-tianyancha",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="天眼查配置",
        path="/system/integrations/tianyancha",
        permission_code="integration:read",
        sort_order=80,
    ),
    _MenuSeed(
        key="integration-tianyancha-update",
        parent_key="integration-tianyancha",
        menu_type=MenuType.BUTTON,
        name="保存天眼查配置",
        path=None,
        permission_code="integration:update",
        sort_order=10,
    ),
    _MenuSeed(
        key="integration-feishu",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="飞书登录配置",
        path="/system/integrations/feishu",
        permission_code="integration_feishu:read",
        sort_order=90,
    ),
    _MenuSeed(
        key="integration-feishu-update",
        parent_key="integration-feishu",
        menu_type=MenuType.BUTTON,
        name="保存飞书配置",
        path=None,
        permission_code="integration_feishu:update",
        sort_order=10,
    ),
    _MenuSeed(
        key="system-setting",
        parent_key="system",
        menu_type=MenuType.MENU,
        name="系统设置",
        path="/system/settings",
        permission_code="system_setting:read",
        sort_order=100,
    ),
    _MenuSeed(
        key="system-setting-update",
        parent_key="system-setting",
        menu_type=MenuType.BUTTON,
        name="保存系统设置",
        path=None,
        permission_code="system_setting:update",
        sort_order=10,
    ),
    _MenuSeed(
        key="customer",
        parent_key=None,
        menu_type=MenuType.DIR,
        name="客户管理",
        path="/customers",
        sort_order=30,
    ),
    _MenuSeed(
        key="customer-list",
        parent_key="customer",
        menu_type=MenuType.MENU,
        name="客户列表",
        path="/customers/list",
        permission_code="customer:read",
        sort_order=10,
    ),
    _MenuSeed(
        key="customer-create",
        parent_key="customer-list",
        menu_type=MenuType.BUTTON,
        name="新增客户",
        path=None,
        permission_code="customer:create",
        sort_order=10,
    ),
    _MenuSeed(
        key="customer-update",
        parent_key="customer-list",
        menu_type=MenuType.BUTTON,
        name="编辑客户",
        path=None,
        permission_code="customer:update",
        sort_order=20,
    ),
    _MenuSeed(
        key="customer-delete",
        parent_key="customer-list",
        menu_type=MenuType.BUTTON,
        name="删除客户",
        path=None,
        permission_code="customer:delete",
        sort_order=30,
    ),
)


async def _get_or_create_pending_department(session: AsyncSession) -> Department:
    statement = select(Department).where(
        Department.is_system.is_(True),
        Department.name == _PENDING_ASSIGNMENT_DEPARTMENT_NAME,
    )
    department = await session.scalar(statement)
    if department is not None:
        return department

    department = Department(
        parent_id=None,
        name=_PENDING_ASSIGNMENT_DEPARTMENT_NAME,
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    return department


async def _get_or_create_role(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    data_scope: DataScope,
    description: str,
) -> Role:
    statement = select(Role).where(
        Role.code == code,
        Role.is_system.is_(True),
    )
    role = await session.scalar(statement)
    if role is not None:
        return role

    role = Role(
        code=code,
        name=name,
        description=description,
        data_scope=data_scope,
        data_scope_include_children=True,
        is_system=True,
        status=RoleStatus.ACTIVE,
    )
    session.add(role)
    await session.flush()
    return role


async def _find_menu(
    session: AsyncSession,
    seed: _MenuSeed,
    *,
    parent_id: int | None,
) -> Menu | None:
    if seed.permission_code is not None:
        return await session.scalar(
            select(Menu).where(Menu.permission_code == seed.permission_code)
        )

    statement = select(Menu).where(
        Menu.type == seed.menu_type,
        Menu.name == seed.name,
        Menu.permission_code.is_(None),
    )
    if parent_id is None:
        statement = statement.where(Menu.parent_id.is_(None))
    else:
        statement = statement.where(Menu.parent_id == parent_id)
    return await session.scalar(statement)


async def _get_or_create_menus(
    session: AsyncSession,
) -> tuple[dict[str, Menu], set[str]]:
    menus_by_key: dict[str, Menu] = {}
    created_keys: set[str] = set()
    for seed in _MENU_SEEDS:
        parent_id = (
            menus_by_key[seed.parent_key].id if seed.parent_key is not None else None
        )
        menu = await _find_menu(session, seed, parent_id=parent_id)
        if menu is None:
            created_keys.add(seed.key)
            payload = MenuCreate(
                parent_id=parent_id,
                type=seed.menu_type,
                name=seed.name,
                path=seed.path,
                permission_code=seed.permission_code,
                sort_order=seed.sort_order,
                visible=True,
            )
            menu = Menu(
                parent_id=payload.parent_id,
                type=payload.type,
                name=payload.name,
                path=payload.path,
                component=payload.component,
                icon=payload.icon,
                permission_code=payload.permission_code,
                sort_order=payload.sort_order,
                visible=payload.visible,
                status=MenuStatus.ACTIVE,
            )
            session.add(menu)
            await session.flush()
        menus_by_key[seed.key] = menu
    return menus_by_key, created_keys


async def _grant_baseline_menus(
    session: AsyncSession,
    *,
    menus_by_key: dict[str, Menu],
    created_keys: set[str],
) -> None:
    targets = [
        menus_by_key[key] for key in ("profile", "directory") if key in created_keys
    ]
    if not targets:
        return

    role_ids = list(
        (
            await session.scalars(
                select(Role.id).where(
                    Role.deleted_at.is_(None),
                    Role.code != _SUPER_ADMIN_ROLE_CODE,
                )
            )
        ).all()
    )
    for role_id in role_ids:
        for menu in targets:
            # This guard handles sequential reruns; the composite PK is the
            # concurrency backstop.
            assigned = await session.scalar(
                select(RoleMenu.c.menu_id).where(
                    RoleMenu.c.role_id == role_id,
                    RoleMenu.c.menu_id == menu.id,
                )
            )
            if assigned is None:
                await session.execute(
                    insert(RoleMenu).values(
                        role_id=role_id,
                        menu_id=menu.id,
                    )
                )


async def _get_or_create_superadmin(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    pending_department: Department,
    super_admin_role: Role,
) -> User:
    normalized_username = normalize_username(username)
    user = await session.scalar(
        select(User).where(User.username == normalized_username)
    )
    if user is not None:
        return user

    user = User(
        feishu_open_id=None,
        username=normalized_username,
        password_hash=hash_password(password),
        nickname="应急超级管理员",
        email=None,
        avatar=None,
        department_id=pending_department.id,
        role_id=super_admin_role.id,
        status=UserStatus.ACTIVE,
        token_version=0,
        last_login_at=None,
    )
    session.add(user)
    await session.flush()
    return user


async def _get_or_create_global_setting(session: AsyncSession) -> SystemSetting:
    setting = await session.scalar(
        select(SystemSetting).where(SystemSetting.setting_key == "global")
    )
    if setting is not None:
        return setting

    setting = SystemSetting(
        setting_key="global",
        name=settings.app_name,
        short_name=None,
        site_url=None,
        logo_url=None,
        favicon_url=None,
        description=None,
        copyright=None,
        icp_number=None,
        updated_by=None,
    )
    session.add(setting)
    await session.flush()
    return setting


async def _ensure_shared_contact_functions(session: AsyncSession) -> None:
    async with serialize_contact_function_name_change(session):
        for name in _SHARED_CONTACT_FUNCTION_NAMES:
            active = list(
                (
                    await session.scalars(
                        select(ContactFunction).where(
                            ContactFunction.name == name,
                            ContactFunction.status == ContactStatus.ACTIVE,
                        )
                    )
                ).all()
            )
            if any(function.customer_id is None for function in active):
                continue
            if active:
                logger.warning(
                    "Skipping shared contact function seed %r: "
                    "an active custom function exists for customer_id=%s",
                    name,
                    active[0].customer_id,
                )
                continue

            session.add(
                ContactFunction(
                    name=name,
                    customer_id=None,
                    scope_key=0,
                    status=ContactStatus.ACTIVE,
                )
            )
            await session.flush()


async def init_data(
    session: AsyncSession,
    *,
    superadmin_username: str,
    superadmin_password: str,
) -> None:
    """创建运行所需的基础数据；已有系统数据不会被重复创建。"""
    try:
        normalize_username(superadmin_username)
    except ValueError as error:
        raise ValueError(f"Invalid initial superadmin username: {error}") from error

    try:
        validate_password_policy(superadmin_password)
    except ValueError as error:
        raise ValueError(f"Invalid initial superadmin password: {error}") from error

    pending_department = await _get_or_create_pending_department(session)
    super_admin_role = await _get_or_create_role(
        session,
        code=_SUPER_ADMIN_ROLE_CODE,
        name="超级管理员",
        data_scope=DataScope.ALL,
        description="系统内置超级管理员角色，拥有全部启用菜单权限",
    )
    await _get_or_create_role(
        session,
        code=_DEFAULT_ROLE_CODE,
        name="默认角色",
        data_scope=DataScope.SELF,
        description="新用户默认角色，仅可查看个人资料",
    )
    menus_by_key, created_keys = await _get_or_create_menus(session)
    await _grant_baseline_menus(
        session,
        menus_by_key=menus_by_key,
        created_keys=created_keys,
    )
    await _get_or_create_superadmin(
        session,
        username=superadmin_username,
        password=superadmin_password,
        pending_department=pending_department,
        super_admin_role=super_admin_role,
    )
    await _get_or_create_global_setting(session)
    await _ensure_shared_contact_functions(session)
    await session.commit()


async def _run() -> None:
    async with session_factory() as session:
        await init_data(
            session,
            superadmin_username=settings.initial_superadmin_username,
            superadmin_password=settings.initial_superadmin_password,
        )


def main() -> None:
    try:
        asyncio.run(_run())
    except ValueError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()


__all__: tuple[str, ...] = ("init_data", "main")
