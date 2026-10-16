import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.redis import get_redis
from app.db.session import get_session, session_factory
from app.main import app as main_app
from app.main import create_app
from app.models.role import DataScope
from app.models.system_metric import SystemMetric
from app.services import health as health_service
from app.services import monitor as monitor_service


class FakeRedis:
    def __init__(
        self,
        *,
        ping_result: bool = True,
        ping_error: Exception | None = None,
    ) -> None:
        self.ping_result = ping_result
        self.ping_error = ping_error

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


def context_with_permissions(*permission_codes: str) -> AuthContext:
    return AuthContext(
        user_id=1,
        role_id=1,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset(permission_codes),
        is_super_admin=False,
    )


def make_endpoint_app(
    *,
    context: AuthContext,
    session: FakeSession | None = None,
) -> FastAPI:
    application = create_app()

    async def override_context() -> AuthContext:
        return context

    async def override_session() -> AsyncIterator[FakeSession]:
        yield session or FakeSession()

    application.dependency_overrides[get_current_context] = override_context
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_redis] = lambda: FakeRedis()
    return application


def test_collect_psutil_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval: 12.5)
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            percent=33.0,
            used=2 * 1024 * 1024,
            total=8 * 1024 * 1024,
        ),
    )
    monkeypatch.setattr(
        psutil,
        "disk_usage",
        lambda mountpoint: SimpleNamespace(
            percent=44.0,
            used=1024 * 1024,
            total=4 * 1024 * 1024,
        ),
    )
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=512 * 1024),
        ),
    )

    snapshot = monitor_service.collect_psutil_snapshot()

    assert snapshot == {
        "cpu_percent": 12.5,
        "memory_percent": 33.0,
        "memory_used_mb": 2.0,
        "memory_total_mb": 8.0,
        "disk_percent": 44.0,
        "disk_used_mb": 1.0,
        "disk_total_mb": 4.0,
        "process_memory_mb": 0.5,
    }


async def test_health_probe_helpers_swallow_failures() -> None:
    assert await health_service.check_database(FakeSession()) is True
    assert (
        await health_service.check_database(
            FakeSession(execute_error=ConnectionError("database unavailable"))
        )
        is False
    )
    assert await health_service.check_redis(FakeRedis()) is True
    assert await health_service.check_redis(FakeRedis(ping_result=False)) is False
    assert (
        await health_service.check_redis(
            FakeRedis(ping_error=ConnectionError("redis unavailable"))
        )
        is False
    )


async def test_sample_once_writes_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        monitor_service,
        "collect_psutil_snapshot",
        lambda: {
            "cpu_percent": 25.0,
            "memory_percent": 50.0,
            "memory_used_mb": 1024.0,
            "memory_total_mb": 2048.0,
            "disk_percent": 20.0,
            "disk_used_mb": 100.0,
            "disk_total_mb": 500.0,
            "process_memory_mb": 64.0,
        },
    )
    monkeypatch.setattr(monitor_service, "redis_client", FakeRedis())

    async with session_factory() as session:
        row = await monitor_service.sample_once(session)

    assert row.id is not None
    assert 0 <= row.cpu_percent <= 100
    assert isinstance(row.db_healthy, bool)
    assert isinstance(row.redis_healthy, bool)
    assert row.memory_total_mb == 2048.0
    assert row.sampled_at is not None

    async with session_factory() as lookup_session:
        statement = select(SystemMetric).where(SystemMetric.id == row.id)
        persisted = await lookup_session.scalar(statement)
        assert persisted is not None
        await lookup_session.delete(persisted)
        await lookup_session.commit()


async def test_sampler_task_cancelled_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_started = asyncio.Event()

    class FakeSessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: Any) -> None:
            return None

    calls = 0

    async def fake_sample_once(session: object) -> None:
        nonlocal calls
        calls += 1
        sample_started.set()

    monkeypatch.setattr(
        monitor_service,
        "session_factory",
        lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(monitor_service, "sample_once", fake_sample_once)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(monitor_service.run_sampler(3600), timeout=1)

    assert sample_started.is_set()
    assert calls == 1


async def test_sampler_retention_does_not_delay_sampling_or_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retention_started = asyncio.Event()
    second_sample_started = asyncio.Event()
    retention_cancelled = asyncio.Event()
    sample_calls = 0
    retention_calls = 0

    class FakeSessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: Any) -> None:
            return None

    async def fake_sample_once(session: object) -> None:
        nonlocal sample_calls
        sample_calls += 1
        if sample_calls == 2:
            second_sample_started.set()

    async def slow_run_retention(session: object) -> dict[str, int]:
        nonlocal retention_calls
        retention_calls += 1
        retention_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            retention_cancelled.set()
        return {}

    monkeypatch.setattr(
        monitor_service,
        "session_factory",
        lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(monitor_service, "sample_once", fake_sample_once)
    monkeypatch.setattr(monitor_service, "run_retention", slow_run_retention)
    monkeypatch.setattr(monitor_service, "_RETENTION_INTERVAL_SECONDS", 0)

    sampler_task = asyncio.create_task(monitor_service.run_sampler(0.05))
    try:
        await asyncio.wait_for(retention_started.wait(), timeout=1)
        await asyncio.wait_for(second_sample_started.wait(), timeout=0.5)
    finally:
        sampler_task.cancel()
        await asyncio.gather(sampler_task, return_exceptions=True)

    assert sample_calls >= 2
    assert retention_calls == 1
    assert retention_cancelled.is_set()


async def test_sampler_survives_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    second_sample_started = asyncio.Event()
    calls = 0

    class FakeSessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: Any) -> None:
            return None

    async def flaky_sample_once(session: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient sampling failure")
        second_sample_started.set()

    monkeypatch.setattr(
        monitor_service,
        "session_factory",
        lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(monitor_service, "sample_once", flaky_sample_once)

    with caplog.at_level(logging.WARNING, logger=monitor_service.__name__):
        sampler_task = asyncio.create_task(monitor_service.run_sampler(0))
        try:
            await asyncio.wait_for(second_sample_started.wait(), timeout=1)
        finally:
            sampler_task.cancel()
            await asyncio.gather(sampler_task, return_exceptions=True)

    assert calls == 2
    assert "sampling" in caplog.text
    assert any(
        record.levelno == logging.WARNING and record.name == monitor_service.__name__
        for record in caplog.records
    )


async def test_current_snapshot_endpoint() -> None:
    allowed_app = make_endpoint_app(
        context=context_with_permissions("monitor:read"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=allowed_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/monitor/system/current")

    assert response.status_code == 200
    data = response.json()["data"]
    assert 0 <= data["cpu_percent"] <= 100
    assert isinstance(data["db_healthy"], bool)
    assert isinstance(data["redis_healthy"], bool)

    denied_app = make_endpoint_app(context=context_with_permissions())
    async with AsyncClient(
        transport=ASGITransport(app=denied_app),
        base_url="http://test",
    ) as client:
        denied_response = await client.get("/api/v1/monitor/system/current")

    assert denied_response.status_code == 403


async def test_history_endpoint_filters_and_pages() -> None:
    base = datetime(2050, 1, 1, 0, 0, 0)
    rows = [
        SystemMetric(
            cpu_percent=float(index),
            memory_percent=50.0,
            memory_used_mb=1024.0,
            memory_total_mb=2048.0,
            disk_percent=20.0,
            disk_used_mb=100.0,
            disk_total_mb=500.0,
            process_memory_mb=64.0,
            db_healthy=True,
            redis_healthy=True,
            sampled_at=base + timedelta(minutes=index),
        )
        for index in range(3)
    ]
    ids: list[int] = []
    try:
        async with session_factory() as session:
            session.add_all(rows)
            await session.commit()
            ids = [row.id for row in rows]

        application = create_app()

        async def override_context() -> AuthContext:
            return context_with_permissions("monitor:read")

        application.dependency_overrides[get_current_context] = override_context
        start_time = (base + timedelta(minutes=1)).isoformat()
        end_time = (base + timedelta(minutes=2)).isoformat()
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            first_page = await client.get(
                "/api/v1/monitor/system/history",
                params={
                    "page": 1,
                    "page_size": 1,
                    "start_time": start_time,
                    "end_time": end_time,
                },
            )
            second_page = await client.get(
                "/api/v1/monitor/system/history",
                params={
                    "page": 2,
                    "page_size": 1,
                    "start_time": start_time,
                    "end_time": end_time,
                },
            )

        assert first_page.status_code == 200
        assert first_page.json()["data"]["total"] == 2
        first_item = first_page.json()["data"]["items"][0]
        assert first_item["id"] == rows[2].id
        assert first_item["db_healthy"] is True
        assert first_item["redis_healthy"] is True
        assert second_page.status_code == 200
        assert second_page.json()["data"]["items"][0]["id"] == rows[1].id
    finally:
        if ids:
            async with session_factory() as cleanup_session:
                await cleanup_session.execute(
                    delete(SystemMetric).where(SystemMetric.id.in_(ids))
                )
                await cleanup_session.commit()


async def test_lifespan_starts_and_cancels_sampler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeRedisLifecycle:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    class FakeEngineLifecycle:
        async def dispose(self) -> None:
            return None

    async def fake_run_sampler(interval_seconds: int) -> None:
        assert interval_seconds == 123
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    import app.main as main_module

    monkeypatch.setattr(main_module, "redis_client", FakeRedisLifecycle())
    monkeypatch.setattr(main_module, "engine", FakeEngineLifecycle())
    monkeypatch.setattr(main_module, "run_sampler", fake_run_sampler)
    monkeypatch.setattr(main_module.settings, "monitor_enabled", True)
    monkeypatch.setattr(main_module.settings, "monitor_sample_interval_seconds", 123)

    async with main_app.router.lifespan_context(main_app):
        await asyncio.wait_for(started.wait(), timeout=1)
        sampler_task = main_app.state.monitor_sampler_task
        assert sampler_task is not None
        assert not sampler_task.done()

    await asyncio.sleep(0)
    assert cancelled.is_set()
    assert main_app.state.monitor_sampler_task is None
