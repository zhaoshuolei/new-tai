from collections.abc import Callable

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.request_context import (
    RequestMeta,
    current_request_meta,
    reset_request_meta,
    set_request_meta,
)
from app.main import create_app


def test_current_request_meta_is_none_when_unset() -> None:
    assert current_request_meta() is None


def test_set_request_meta_exposes_fields_until_reset() -> None:
    meta = RequestMeta(
        request_id="req-1",
        method="GET",
        path="/widgets",
        ip="127.0.0.1",
        user_agent="pytest-agent",
    )

    token = set_request_meta(meta)

    try:
        assert current_request_meta() is meta
    finally:
        reset_request_meta(token)

    assert current_request_meta() is None


def _build_client(
    app: FastAPI,
    wrap: Callable[[ASGIApp], ASGIApp] | None = None,
) -> AsyncClient:
    asgi_app: ASGIApp = app
    if wrap is not None:
        asgi_app = wrap(asgi_app)
    return AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://test",
    )


async def test_middleware_sets_request_meta_visible_in_handler() -> None:
    test_app = create_app()

    @test_app.get("/request-meta")
    async def request_meta_route(request: Request) -> dict[str, str | None]:
        meta = current_request_meta()
        request.state.request_meta = meta
        assert meta is not None
        return {
            "request_id": meta.request_id,
            "method": meta.method,
            "path": meta.path,
            "ip": meta.ip,
            "user_agent": meta.user_agent,
        }

    async with _build_client(test_app) as client:
        response = await client.get(
            "/request-meta",
            headers={"X-Request-Id": "inbound-id", "User-Agent": "pytest-agent"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "inbound-id"
    assert body["request_id"] == response.headers["X-Request-Id"]
    assert body["method"] == "GET"
    assert body["path"] == "/request-meta"
    assert body["ip"] == "127.0.0.1"
    assert body["user_agent"] == "pytest-agent"


async def test_middleware_truncates_user_agent_to_512_characters() -> None:
    test_app = create_app()
    long_user_agent = "a" * 600

    @test_app.get("/request-meta-user-agent")
    async def user_agent_route() -> dict[str, int]:
        meta = current_request_meta()
        assert meta is not None
        assert meta.user_agent is not None
        return {"length": len(meta.user_agent)}

    async with _build_client(test_app) as client:
        response = await client.get(
            "/request-meta-user-agent",
            headers={"User-Agent": long_user_agent},
        )

    assert response.json()["length"] == 512


async def test_middleware_ip_is_none_when_client_is_missing() -> None:
    test_app = create_app()

    @test_app.get("/request-meta-ip")
    async def ip_route() -> dict[str, str | None]:
        meta = current_request_meta()
        assert meta is not None
        return {"ip": meta.ip}

    def drop_client(app: ASGIApp) -> ASGIApp:
        async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
            scope = dict(scope)
            scope["client"] = None
            await app(scope, receive, send)

        return wrapped

    async with _build_client(test_app, wrap=drop_client) as client:
        response = await client.get("/request-meta-ip")

    assert response.json()["ip"] is None


async def test_middleware_resets_request_meta_after_request() -> None:
    test_app = create_app()

    @test_app.get("/request-meta-cleanup")
    async def cleanup_route() -> dict[str, str]:
        return {"status": "ok"}

    async with _build_client(test_app) as client:
        response = await client.get("/request-meta-cleanup")

    assert response.status_code == 200
    assert current_request_meta() is None
