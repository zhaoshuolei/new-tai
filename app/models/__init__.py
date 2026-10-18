"""领域模型包，模型模块导入后向 Base.metadata 注册表定义。"""

from app.models.audit_log import AuditLog, AuditResult
from app.models.contact import (
    ContactFunction,
    ContactFunctionNameLock,
    ContactStatus,
    CustomerContact,
)
from app.models.customer import Customer, CustomerSource
from app.models.data_management import (
    DataCurrentSnapshot,
    DataEnumOption,
    DataFieldDefinition,
    DataFieldRole,
    DataFieldType,
    DataImportError,
    DataImportJob,
    DataImportSource,
    DataImportStatus,
    DataRecord,
    DataRecordValue,
    DataSnapshot,
    DataSnapshotStatus,
    DataTableDefinition,
    DataTableStatus,
)
from app.models.department import Department, DepartmentStatus
from app.models.exception_log import ExceptionLevel, ExceptionLog
from app.models.follow_up import (
    ContactFollowUp,
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)
from app.models.integration_config import IntegrationConfig
from app.models.login_log import LoginLog, LoginResultEnum, LoginType
from app.models.menu import Menu, MenuStatus, MenuType
from app.models.role import (
    DataScope,
    Role,
    RoleMenu,
    RoleStatus,
)
from app.models.role_department import RoleDepartment
from app.models.system_metric import SystemMetric
from app.models.system_setting import SystemSetting
from app.models.user import User, UserStatus
from app.models.user_profile import PUBLIC_PROFILE_FIELD_KEYS, UserProfile

__all__: tuple[str, ...] = (
    "AuditLog",
    "AuditResult",
    "ContactFollowUp",
    "ContactFollowUpAttachment",
    "ContactFollowUpMethod",
    "ContactFunction",
    "ContactFunctionNameLock",
    "ContactStatus",
    "Customer",
    "CustomerContact",
    "CustomerSource",
    "DataCurrentSnapshot",
    "DataEnumOption",
    "DataFieldDefinition",
    "DataFieldRole",
    "DataFieldType",
    "DataImportError",
    "DataImportJob",
    "DataImportSource",
    "DataImportStatus",
    "DataRecord",
    "DataRecordValue",
    "DataScope",
    "DataSnapshot",
    "DataSnapshotStatus",
    "DataTableDefinition",
    "DataTableStatus",
    "Department",
    "DepartmentStatus",
    "ExceptionLevel",
    "ExceptionLog",
    "IntegrationConfig",
    "LoginLog",
    "LoginResultEnum",
    "LoginType",
    "Menu",
    "MenuStatus",
    "MenuType",
    "PUBLIC_PROFILE_FIELD_KEYS",
    "Role",
    "RoleDepartment",
    "RoleMenu",
    "RoleStatus",
    "SystemMetric",
    "SystemSetting",
    "User",
    "UserProfile",
    "UserStatus",
)
