from collections.abc import AsyncGenerator, Callable

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health as health_module
from app.api.health import router
from app.db import session as session_module

SessionDependency = Callable[[], AsyncGenerator[AsyncSession]]


class FakeRedis:
    def __init__(
        self,
        *,
        ping_error: Exception | None = None,
        ping_result: bool = True,
    ) -> None:
        self.ping_error = ping_error
        self.ping_result = ping_result

    async def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return self.ping_result


class FakeSession:
    def __init__(self, *, execute_error: Exception | None = None) -> None:
        self.execute_error = execute_error

    async def execute(self, statement: object) -> None:
        if self.execute_error is not None:
            raise self.execute_error


async def fake_session() -> AsyncGenerator[AsyncSession]:
    yield FakeSession()  # type: ignore[misc]


async def failing_session() -> AsyncGenerator[AsyncSession]:
    yield FakeSession(  # type: ignore[misc]
        execute_error=ConnectionError("database unavailable"),
    )


def make_test_app(
    session_dependency: SessionDependency = fake_session,
) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[session_module.get_session] = session_dependency
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
async def test_readiness_503_when_database_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health_module, "redis_client", FakeRedis())

    async with AsyncClient(
        transport=ASGITransport(
            app=make_test_app(session_dependency=failing_session),
        ),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


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


@pytest.mark.asyncio
async def test_readiness_503_when_redis_ping_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_module,
        "redis_client",
        FakeRedis(ping_result=False),
    )

    async with AsyncClient(
        transport=ASGITransport(app=make_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
