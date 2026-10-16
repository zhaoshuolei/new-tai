import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.errors import BusinessError, ErrorCode

logger = logging.getLogger(__name__)

_HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_PARAMETER,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    422: ErrorCode.INVALID_PARAMETER,
    429: ErrorCode.RATE_LIMITED,
}


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id

    header_request_id = request.headers.get("X-Request-Id")
    if header_request_id:
        return header_request_id

    return str(uuid4())


def _error_response(
    request: Request,
    *,
    code: int,
    message: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": _request_id(request),
        },
    )


async def business_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, BusinessError):
        raise exc

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

    return _error_response(
        request,
        code=_HTTP_ERROR_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR),
        message=str(exc.detail),
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
    logger.error("Unhandled request exception", exc_info=exc)
    return _error_response(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        message="Internal server error",
        status_code=500,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(BusinessError, business_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__: tuple[str, ...] = ("register_exception_handlers",)
