from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health as health_module
from app.api.health import router
from app.db import session as session_module


class FakeRedis:
    def __init__(self, *, ping_error: Exception | None = None) -> None:
        self.ping_error = ping_error

    async def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True


class FakeSession:
    async def execute(self, statement: object) -> None:
        return None


async def fake_session() -> AsyncGenerator[AsyncSession]:
    yield FakeSession()  # type: ignore[misc]


def make_test_app() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[session_module.get_session] = fake_session
    return test_app


@pytest.mark.asyncio
async def test_liveness_ok() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=make_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_ok_when_db_and_redis_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health_module, "redis_client", FakeRedis())

    async with AsyncClient(
        transport=ASGITransport(app=make_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_503_when_redis_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_module,
        "redis_client",
        FakeRedis(ping_error=ConnectionError("redis unavailable")),
    )

    async with AsyncClient(
        transport=ASGITransport(app=make_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
