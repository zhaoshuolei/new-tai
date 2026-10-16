"""功能权限依赖。"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode


def require_permission(
    code: str,
) -> Callable[..., Awaitable[AuthContext]]:
    """返回校验指定权限码的 FastAPI 依赖。"""
    from app.core.deps import get_current_context

    async def dependency(
        context: Annotated[AuthContext, Depends(get_current_context)],
    ) -> AuthContext:
        if code not in context.permission_codes:
            raise BusinessError(
                ErrorCode.FORBIDDEN,
                "Permission denied",
                http_status=403,
            )
        return context

    return dependency


__all__: tuple[str, ...] = ("require_permission",)
