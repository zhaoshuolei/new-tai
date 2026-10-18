from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main
from app.main import app


class FakeRedis:
    def __init__(self, *, ping_error: Exception | None = None) -> None:
        self.ping_error = ping_error
        self.ping_calls = 0
        self.close_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def aclose(self) -> None:
        self.close_calls += 1


class FakeEngine:
    def __init__(self, *, dispose_error: Exception | None = None) -> None:
        self.dispose_error = dispose_error
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1
        if self.dispose_error is not None:
            raise self.dispose_error


def patch_lifespan_resources(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: FakeRedis,
    engine: FakeEngine,
) -> None:
    monkeypatch.setattr(main, "redis_client", redis_client)
    monkeypatch.setattr(main, "engine", engine)


@pytest.mark.asyncio
async def test_app_boots_and_serves_openapi() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "tianxiadmin"


def test_openapi_declares_business_domain_tags() -> None:
    tag_names = {tag["name"] for tag in app.openapi()["tags"]}

    assert tag_names == {
        "认证",
        "用户",
        "角色",
        "菜单",
        "部门",
        "通讯录",
        "客户",
        "客户跟进",
        "集成",
        "系统设置",
        "个人资料",
        "健康",
        "数据管理",
    }


@pytest.mark.asyncio
async def test_lifespan_pings_and_releases_shared_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    engine = FakeEngine()
    patch_lifespan_resources(monkeypatch, redis_client, engine)

    assert main.redis_client is redis_client
    assert main.engine is engine

    async with app.router.lifespan_context(app):
        assert app.state.redis_client is redis_client
        assert app.state.db_engine is engine

    assert redis_client.ping_calls == 1
    assert redis_client.close_calls == 1
    assert engine.dispose_calls == 1
    assert app.state.redis_client is None
    assert app.state.db_engine is None


@pytest.mark.asyncio
async def test_lifespan_fails_startup_when_redis_ping_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis(ping_error=ConnectionError("redis unavailable"))
    engine = FakeEngine()
    patch_lifespan_resources(monkeypatch, redis_client, engine)

    with pytest.raises(ConnectionError, match="redis unavailable"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan should not yield")

    assert redis_client.ping_calls == 1
    assert redis_client.close_calls == 1
    assert engine.dispose_calls == 1
    assert app.state.redis_client is None
    assert app.state.db_engine is None


@pytest.mark.asyncio
async def test_lifespan_disposes_engine_when_redis_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    engine = FakeEngine()

    async def failing_close() -> None:
        redis_client.close_calls += 1
        raise RuntimeError("redis close failed")

    redis_client.aclose = failing_close
    patch_lifespan_resources(monkeypatch, redis_client, engine)

    with pytest.raises(RuntimeError, match="redis close failed"):
        async with app.router.lifespan_context(app):
            pass

    assert redis_client.close_calls == 1
    assert engine.dispose_calls == 1
    assert app.state.redis_client is None
    assert app.state.db_engine is None


@pytest.mark.asyncio
async def test_lifespan_closes_redis_when_engine_dispose_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    engine = FakeEngine(dispose_error=RuntimeError("database dispose failed"))
    patch_lifespan_resources(monkeypatch, redis_client, engine)

    with pytest.raises(RuntimeError, match="database dispose failed"):
        async with app.router.lifespan_context(app):
            pass

    assert redis_client.close_calls == 1
    assert engine.dispose_calls == 1
    assert app.state.redis_client is None
    assert app.state.db_engine is None


def test_pre_commit_mypy_hook_uses_project_environment() -> None:
    config_text = Path(".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "language: system" in config_text
    assert "entry: .venv/bin/mypy app" in config_text
    assert "pass_filenames: false" in config_text
