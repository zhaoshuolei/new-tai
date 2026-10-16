import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.core.config import settings
from app.core.logging import (
    current_request_id,
    reset_request_id,
    set_request_id,
)
from app.core.request_context import (
    USER_AGENT_MAX_LENGTH,
    RequestMeta,
    reset_request_meta,
    set_request_meta,
)

logger = logging.getLogger("app.request")

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _request_user_id(request: Request) -> object:
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        return user_id

    user = getattr(request.state, "user", None)
    return getattr(user, "id", None)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid4())
        request.state.request_id = request_id
        request_id_token = set_request_id(request_id)
        user_agent = request.headers.get("user-agent")
        request_meta_token = set_request_meta(
            RequestMeta(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                ip=request.client.host if request.client is not None else None,
                user_agent=(
                    user_agent[:USER_AGENT_MAX_LENGTH]
                    if user_agent is not None
                    else None
                ),
            ),
        )
        started_at = perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            try:
                access_fields = {
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round(
                        (perf_counter() - started_at) * 1000,
                        3,
                    ),
                    "user_id": _request_user_id(request),
                }
                logger.info(
                    "HTTP request completed",
                    extra={
                        **access_fields,
                        "extra_fields": access_fields,
                    },
                )
            finally:
                reset_request_meta(request_meta_token)
                reset_request_id(request_id_token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        for header_name, header_value in SECURITY_HEADERS.items():
            response.headers[header_name] = header_value
        return response


def configure_security(app: FastAPI) -> None:
    cors_origins = list(settings.cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)


__all__: tuple[str, ...] = (
    "SECURITY_HEADERS",
    "RequestContextMiddleware",
    "configure_security",
    "current_request_id",
)
