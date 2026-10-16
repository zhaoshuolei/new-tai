from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode


async def get_current_context() -> AuthContext:
    raise BusinessError(
        ErrorCode.UNAUTHENTICATED,
        "Authentication required",
        http_status=401,
    )


__all__: tuple[str, ...] = ("get_current_context",)
