from enum import IntEnum


class ErrorCode(IntEnum):
    UNAUTHENTICATED = 1000
    INVALID_CREDENTIALS = 1001
    ACCOUNT_DISABLED = 1002
    TOKEN_EXPIRED = 1003
    TOKEN_INVALID = 1004
    RATE_LIMITED = 1005
    INVALID_PARAMETER = 1006
    INTERNAL_ERROR = 1007
    FORBIDDEN = 1010

    USER_NOT_FOUND = 2000
    USERNAME_EXISTS = 2001
    CANNOT_MODIFY_SELF = 2002
    CANNOT_GRANT_SUPER_ADMIN = 2003
    LAST_SUPER_ADMIN = 2004

    ROLE_NOT_FOUND = 3000
    SYSTEM_ROLE_IMMUTABLE = 3001
    ROLE_IN_USE = 3002

    MENU_NOT_FOUND = 4000
    INVALID_MENU_HIERARCHY = 4001
    PERMISSION_CODE_EXISTS = 4002

    DEPT_NOT_FOUND = 5000
    DEPT_HAS_CHILDREN = 5001
    DEPT_HAS_USERS = 5002
    INVALID_DEPT_MOVE = 5003
    SYSTEM_DEPT_IMMUTABLE = 5004


class BusinessError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


__all__: tuple[str, ...] = ("BusinessError", "ErrorCode")
