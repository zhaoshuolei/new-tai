import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.logging import (
    JsonFormatter,
    LocalFormatter,
    current_request_id,
    mask_sensitive_text,
)
from app.main import create_app


async def test_response_has_request_id_header() -> None:
    test_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["X-Request-Id"]


async def test_inbound_request_id_is_preserved() -> None:
    test_app = create_app()
    request_id = "inbound-request-id"

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health/live",
            headers={"X-Request-Id": request_id},
        )

    assert response.headers["X-Request-Id"] == request_id


async def test_request_id_context_is_available_to_route() -> None:
    test_app = create_app()

    @test_app.get("/current-request-id")
    async def request_id_route() -> dict[str, str]:
        return {"request_id": current_request_id()}

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/current-request-id")

    assert response.json()["request_id"] == response.headers["X-Request-Id"]


async def test_raised_request_keeps_request_id_in_header_and_envelope() -> None:
    test_app = create_app()

    @test_app.get("/unhandled-with-request-id")
    async def unhandled() -> None:
        raise RuntimeError("sensitive internal detail")

    async with AsyncClient(
        transport=ASGITransport(app=test_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/unhandled-with-request-id",
            headers={"X-Request-Id": "failed-request-id"},
        )

    assert response.status_code == 500
    assert response.headers["X-Request-Id"] == "failed-request-id"
    assert response.json()["request_id"] == "failed-request-id"


def test_sensitive_fields_are_masked_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    original_formatter = caplog.handler.formatter
    caplog.handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.sensitive")
    try:
        logger.info(
            "credentials",
            extra={
                "extra_fields": {
                    "password": "secret-password",
                    "nested": {"access_token": "secret-token"},
                    "safe": "visible",
                }
            },
        )
    finally:
        caplog.handler.setFormatter(original_formatter)

    payload = json.loads(caplog.text)

    assert payload["password"] == "***"
    assert payload["nested"]["access_token"] == "***"
    assert payload["safe"] == "visible"
    assert "secret-password" not in caplog.text
    assert "secret-token" not in caplog.text
    assert mask_sensitive_text("password=secret with spaces") == "password=***"


async def test_unhandled_exception_log_uses_response_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_app = create_app()

    @test_app.get("/unhandled-request-id")
    async def unhandled() -> None:
        raise RuntimeError("sensitive internal detail")

    with caplog.at_level(logging.ERROR, logger="app.core.exception_handlers"):
        async with AsyncClient(
            transport=ASGITransport(app=test_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/unhandled-request-id",
                headers={"X-Request-Id": "logged-request-id"},
            )

    records = [
        record
        for record in caplog.records
        if record.name == "app.core.exception_handlers"
    ]
    assert len(records) == 1
    assert records[0].request_id == response.headers["X-Request-Id"]
    assert records[0].request_id == response.json()["request_id"]


async def test_local_access_log_contains_required_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_app = create_app()
    caplog.clear()
    original_formatter = caplog.handler.formatter
    caplog.handler.setFormatter(LocalFormatter())

    try:
        with caplog.at_level(logging.INFO, logger="app.request"):
            async with AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url="http://test",
            ) as client:
                await client.get("/health/live")
        output = caplog.text
    finally:
        caplog.handler.setFormatter(original_formatter)

    assert "method='GET'" in output
    assert "path='/health/live'" in output
    assert "status_code=200" in output
    assert "duration_ms=" in output
    assert "user_id=None" in output


async def test_access_log_contains_request_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_app = create_app()

    @test_app.get("/logged")
    async def logged_route() -> dict[str, str]:
        return {"status": "ok"}

    with caplog.at_level(logging.INFO, logger="app.request"):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            response = await client.get("/logged")

    records = [record for record in caplog.records if record.name == "app.request"]
    assert len(records) == 1
    assert records[0].method == "GET"
    assert records[0].path == "/logged"
    assert records[0].status_code == response.status_code
    assert isinstance(records[0].duration_ms, float)
    assert records[0].user_id is None


async def test_cors_allows_whitelisted_origin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_origin = "https://admin.example.com"
    monkeypatch.setattr(settings, "cors_origins", [allowed_origin])
    test_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        allowed = await client.get(
            "/health/live",
            headers={"Origin": allowed_origin},
        )
        denied = await client.get(
            "/health/live",
            headers={"Origin": "https://evil.example.com"},
        )

    assert allowed.headers["Access-Control-Allow-Origin"] == allowed_origin
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    assert "Access-Control-Allow-Origin" not in denied.headers


async def test_wildcard_cors_never_uses_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cors_origins", ["*"])
    test_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health/live",
            headers={"Origin": "https://admin.example.com"},
        )

    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in response.headers


async def test_security_headers_present_on_response() -> None:
    test_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_configure_security_is_available_as_app_factory_helper() -> None:
    from app.core.middleware import configure_security

    application = FastAPI()
    configure_security(application)

    assert application.user_middleware
