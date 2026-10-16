from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.errors import BusinessError, ErrorCode
from app.core.response import Page, success
from app.main import create_app


def test_success_returns_unified_envelope() -> None:
    assert success({"id": 7}, message="created") == {
        "code": 0,
        "message": "created",
        "data": {"id": 7},
    }


def test_page_serializes_pagination_metadata() -> None:
    page = Page[int](items=[1, 2], total=5, page=1, page_size=2)

    assert page.model_dump() == {
        "items": [1, 2],
        "total": 5,
        "page": 1,
        "page_size": 2,
    }


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
