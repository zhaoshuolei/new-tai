"""操作审计写入与 service 装饰器。"""

import inspect
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.best_effort import run_best_effort
from app.core.errors import BusinessError, ErrorCode
from app.core.request_context import (
    RequestMeta,
    bounded_request_id,
    current_request_meta,
)
from app.db.session import session_factory
from app.models.audit_log import AuditLog, AuditResult
from app.repositories import audit_log as audit_log_repository

DENIED_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.CANNOT_MODIFY_SELF,
        ErrorCode.CANNOT_GRANT_SUPER_ADMIN,
        ErrorCode.LAST_SUPER_ADMIN,
        ErrorCode.SYSTEM_ROLE_IMMUTABLE,
        ErrorCode.INVALID_MENU_HIERARCHY,
        ErrorCode.INVALID_DEPT_MOVE,
        ErrorCode.SYSTEM_DEPT_IMMUTABLE,
    }
)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def is_denial(error: BusinessError) -> bool:
    """Return whether a business error represents an authorization denial."""
    return error.http_status == 403 or error.code in DENIED_ERROR_CODES


def actor_from_context(context: AuthContext | None) -> ActorSnapshot | None:
    """Build an actor snapshot from an auth context, or None when unauthenticated."""
    if context is None:
        return None
    return ActorSnapshot(
        user_id=context.user_id,
        username=context.username,
        nickname=context.nickname,
    )


async def record_audit_entry(
    *,
    action: str,
    resource_type: str,
    resource_id: int | None,
    result: AuditResult,
    reason: str | None,
    actor: ActorSnapshot | None,
    request_meta: RequestMeta | None,
) -> None:
    """Persist one audit row in an independent best-effort transaction."""

    async def write_entry() -> None:
        row = AuditLog(
            user_id=actor.user_id if actor is not None else None,
            actor_username=actor.username if actor is not None else None,
            actor_nickname=actor.nickname if actor is not None else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            reason=reason,
            method=request_meta.method if request_meta is not None else None,
            path=request_meta.path if request_meta is not None else None,
            ip=request_meta.ip if request_meta is not None else None,
            request_id=bounded_request_id(
                request_meta.request_id if request_meta is not None else ""
            ),
        )
        async with session_factory() as session:
            await audit_log_repository.add(session, row)
            await session.commit()

    await run_best_effort(
        write_entry,
        description=f"record audit entry for {action}",
    )


def _resource_id(
    *,
    bound_arguments: dict[str, Any],
    result: Any,
    resource_id_param: str | None,
    resource_id_attr: str | None,
) -> int | None:
    if resource_id_attr is not None:
        value = getattr(result, resource_id_attr, None)
    elif resource_id_param is not None:
        value = bound_arguments.get(resource_id_param)
    else:
        value = None
    return value if isinstance(value, int) else None


def _error_reason(error: BusinessError) -> str:
    try:
        return ErrorCode(error.code).name
    except ValueError:
        return str(error.code)


def audited(
    *,
    action: str,
    resource_type: str,
    resource_id_param: str | None = None,
    resource_id_attr: str | None = None,
) -> Callable[
    [Callable[_P, Awaitable[_R]]],
    Callable[_P, Awaitable[_R]],
]:
    """Record the success or security denial of an async service operation."""

    def decorator(
        func: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R]]:
        signature = inspect.signature(func)

        @wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            bound = signature.bind_partial(*args, **kwargs)
            raw_context = bound.arguments.get("context")
            context = raw_context if isinstance(raw_context, AuthContext) else None
            request_meta = current_request_meta()

            try:
                result = await func(*args, **kwargs)
            except BusinessError as error:
                if is_denial(error):
                    await record_audit_entry(
                        action=action,
                        resource_type=resource_type,
                        resource_id=_resource_id(
                            bound_arguments=bound.arguments,
                            result=None,
                            resource_id_param=resource_id_param,
                            resource_id_attr=resource_id_attr,
                        ),
                        result=AuditResult.DENIED,
                        reason=_error_reason(error),
                        actor=actor_from_context(context),
                        request_meta=request_meta,
                    )
                    setattr(error, "audit_recorded", True)  # noqa: B010
                raise

            await record_audit_entry(
                action=action,
                resource_type=resource_type,
                resource_id=_resource_id(
                    bound_arguments=bound.arguments,
                    result=result,
                    resource_id_param=resource_id_param,
                    resource_id_attr=resource_id_attr,
                ),
                result=AuditResult.SUCCESS,
                reason=None,
                actor=actor_from_context(context),
                request_meta=request_meta,
            )
            return result

        return wrapper

    return decorator


__all__: tuple[str, ...] = (
    "DENIED_ERROR_CODES",
    "actor_from_context",
    "audited",
    "is_denial",
    "record_audit_entry",
)
