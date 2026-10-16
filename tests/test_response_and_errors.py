import logging

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.errors import BusinessError, ErrorCode
from app.core.response import MAX_PAGE_SIZE, Page, success
from app.main import create_app


def test_success_returns_unified_envelope() -> None:
    assert success({"id": 7}, message="created") == {
        "code": 0,
        "message": "created",
        "data": {"id": 7},
    }


def test_business_routes_expose_success_envelope_in_openapi() -> None:
    document = create_app().openapi()
    components = document["components"]["schemas"]

    for path, path_item in document["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = operation["responses"]
            success_response = next(
                response
                for status_code, response in responses.items()
                if status_code.startswith("2")
            )
            schema = success_response["content"]["application/json"]["schema"]
            if "$ref" in schema:
                schema = components[schema["$ref"].rsplit("/", 1)[-1]]
            assert {"code", "message", "data"} <= set(schema["properties"])


def test_page_serializes_pagination_metadata() -> None:
    page = Page[int](items=[1, 2], total=5, page=1, page_size=2)

    assert page.model_dump() == {
        "items": [1, 2],
        "total": 5,
        "page": 1,
        "page_size": 2,
    }


def test_page_enforces_global_pagination_bounds() -> None:
    page = Page[int](
        items=[1],
        total=1,
        page=MAX_PAGE_SIZE,
        page_size=MAX_PAGE_SIZE,
    )

    assert page.page == MAX_PAGE_SIZE
    assert page.page_size == MAX_PAGE_SIZE

    with pytest.raises(ValidationError):
        Page[int](
            items=[],
            total=0,
            page=1,
            page_size=MAX_PAGE_SIZE + 1,
        )

    with pytest.raises(ValidationError):
        Page[int](items=[], total=0, page=0, page_size=1)

    with pytest.raises(ValidationError):
        Page[int](
            items=[],
            total=0,
            page=MAX_PAGE_SIZE + 1,
            page_size=1,
        )


async def test_http_exception_returns_error_envelope() -> None:
    test_app = create_app()

    @test_app.get("/http-error")
    async def http_error() -> None:
        raise HTTPException(status_code=401, detail="Authentication required")

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/http-error")

    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == ErrorCode.UNAUTHENTICATED
    assert payload["message"] == "Authentication required"
    assert payload["request_id"]
    assert set(payload) == {"code", "message", "request_id"}


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (404, ErrorCode.NOT_FOUND),
        (409, ErrorCode.CONFLICT),
        (418, ErrorCode.INTERNAL_ERROR),
    ],
)
async def test_http_exception_status_mapping(
    status_code: int,
    expected_code: ErrorCode,
) -> None:
    test_app = create_app()

    @test_app.get("/http-error")
    async def http_error() -> None:
        raise HTTPException(
            status_code=status_code,
            detail=f"detail-{status_code}",
        )

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/http-error")

    assert response.status_code == status_code
    payload = response.json()
    assert payload["code"] == expected_code
    assert payload["message"] == f"detail-{status_code}"
    assert payload["request_id"]


async def test_http_server_error_is_logged_without_leaking_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_app = create_app()
    sensitive_detail = "database credentials leaked"

    @test_app.get("/http-server-error")
    async def http_server_error() -> None:
        raise HTTPException(status_code=503, detail=sensitive_detail)

    with caplog.at_level(logging.ERROR, logger="app.core.exception_handlers"):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://test",
        ) as client:
            response = await client.get("/http-server-error")

    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == ErrorCode.INTERNAL_ERROR
    assert payload["message"] == "Internal server error"
    assert payload["request_id"]
    assert sensitive_detail not in response.text
    assert "Traceback" not in response.text

    records = [
        record
        for record in caplog.records
        if record.name == "app.core.exception_handlers"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None


async def test_unhandled_exception_returns_500_without_stack() -> None:
    test_app = create_app()

    @test_app.get("/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("sensitive internal detail")

    async with AsyncClient(
        transport=ASGITransport(app=test_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/unhandled")

    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == ErrorCode.INTERNAL_ERROR
    assert payload["message"] == "Internal server error"
    assert payload["request_id"]
    assert set(payload) == {"code", "message", "request_id"}
    assert "sensitive internal detail" not in response.text
    assert "Traceback" not in response.text


async def test_validation_error_returns_422_without_stack() -> None:
    test_app = create_app()

    @test_app.get("/items")
    async def list_items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/items", params={"limit": "invalid"})

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == ErrorCode.INVALID_PARAMETER
    assert payload["message"] == "Invalid request parameters"
    assert payload["request_id"]
    assert set(payload) == {"code", "message", "request_id"}
    assert "Traceback" not in response.text
    assert "loc" not in response.text


async def test_business_error_returns_envelope() -> None:
    test_app = create_app()

    @test_app.get("/business-error")
    async def business_error() -> None:
        raise BusinessError(
            ErrorCode.FORBIDDEN,
            "permission denied",
            http_status=403,
        )

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/business-error")

    assert response.status_code == 403
    payload = response.json()
    assert payload["code"] == ErrorCode.FORBIDDEN
    assert payload["message"] == "permission denied"
    assert payload["request_id"]
