import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.audit_meta import get_audit_route_meta
from app.core.errors import BusinessError, ErrorCode
from app.core.logging import current_request_id
from app.core.middleware import SECURITY_HEADERS
from app.core.request_context import (
    USER_AGENT_MAX_LENGTH,
    RequestMeta,
    current_request_meta,
)
from app.models.audit_log import AuditResult
from app.models.exception_log import ExceptionLevel
from app.services.audit import is_denial, record_audit_entry
from app.services.exception_log import record_exception

logger = logging.getLogger(__name__)

_HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_PARAMETER,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.INVALID_PARAMETER,
    429: ErrorCode.RATE_LIMITED,
}

_INTERNAL_SERVER_ERROR_MESSAGE = "Internal server error"


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id

    context_request_id = current_request_id()
    if context_request_id:
        return context_request_id

    header_request_id = request.headers.get("X-Request-Id")
    if header_request_id:
        return header_request_id

    return str(uuid4())


def _request_user_id(request: Request) -> int | None:
    user_id = getattr(request.state, "user_id", None)
    return user_id if isinstance(user_id, int) else None


def _exception_request_meta(request: Request) -> RequestMeta:
    """Return the request context, rebuilding it when outer middleware reset it."""
    meta = current_request_meta()
    if meta is not None:
        return meta

    user_agent = request.headers.get("user-agent")
    return RequestMeta(
        request_id=_request_id(request),
        method=request.method,
        path=request.url.path,
        ip=request.client.host if request.client is not None else None,
        user_agent=(
            user_agent[:USER_AGENT_MAX_LENGTH] if user_agent is not None else None
        ),
    )


def _error_response(
    request: Request,
    *,
    code: int,
    message: str,
    status_code: int,
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": request_id,
        },
        headers={
            "X-Request-Id": request_id,
            **SECURITY_HEADERS,
        },
    )


def _error_reason(error: BusinessError) -> str:
    try:
        return ErrorCode(error.code).name
    except ValueError:
        return str(error.code)


def _path_resource_id(request: Request) -> int | None:
    path_params = request.path_params
    candidate: object | None = None
    for name, value in path_params.items():
        if name == "resource_id" or name.endswith("_id"):
            candidate = value
            break
    if candidate is None and len(path_params) == 1:
        candidate = next(iter(path_params.values()))
    if candidate is None:
        return None

    try:
        return int(str(candidate))
    except TypeError, ValueError:
        return None


async def _record_dependency_denial(
    request: Request,
    exc: BusinessError,
) -> None:
    """Record a pre-service denial unless the service hook already did."""
    if not is_denial(exc) or getattr(exc, "audit_recorded", False):
        return

    # A 404 without a matched route has no scope["route"]; nothing to look up.
    route_path = getattr(request.scope.get("route"), "path", None)
    if not isinstance(route_path, str):
        return

    meta = get_audit_route_meta(request.method, route_path)
    if meta is None:
        return

    actor = getattr(request.state, "actor", None)
    await record_audit_entry(
        action=meta.action,
        resource_type=meta.resource_type,
        resource_id=_path_resource_id(request),
        result=AuditResult.DENIED,
        reason=_error_reason(exc),
        actor=actor,
        request_meta=current_request_meta(),
    )
    setattr(exc, "audit_recorded", True)  # noqa: B010


async def business_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, BusinessError):
        raise exc

    await _record_dependency_denial(request, exc)

    if exc.http_status >= 500:
        await record_exception(
            exc=exc,
            level=ExceptionLevel.ERROR,
            request_meta=_exception_request_meta(request),
            user_id=_request_user_id(request),
            status_code=exc.http_status,
        )

    return _error_response(
        request,
        code=exc.code,
        message=exc.message,
        status_code=exc.http_status,
    )


async def http_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc

    if exc.status_code >= 500:
        logger.error(
            "HTTP server exception",
            exc_info=exc,
            extra={"request_id": _request_id(request)},
        )
        await record_exception(
            exc=exc,
            level=ExceptionLevel.ERROR,
            request_meta=_exception_request_meta(request),
            user_id=_request_user_id(request),
            status_code=exc.status_code,
        )
        message = _INTERNAL_SERVER_ERROR_MESSAGE
    else:
        message = str(exc.detail)

    return _error_response(
        request,
        code=_HTTP_ERROR_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR),
        message=message,
        status_code=exc.status_code,
    )


async def validation_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc

    return _error_response(
        request,
        code=ErrorCode.INVALID_PARAMETER,
        message="Invalid request parameters",
        status_code=422,
    )


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    logger.error(
        "Unhandled request exception",
        exc_info=exc,
        extra={"request_id": _request_id(request)},
    )
    await record_exception(
        exc=exc,
        level=ExceptionLevel.ERROR,
        request_meta=_exception_request_meta(request),
        user_id=_request_user_id(request),
        status_code=500,
    )
    return _error_response(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        message=_INTERNAL_SERVER_ERROR_MESSAGE,
        status_code=500,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(BusinessError, business_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__: tuple[str, ...] = ("register_exception_handlers",)
