"""Durable data import worker loop and entrypoint tests."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.repositories import data_import as data_import_repository
from app.services import audit as audit_service
from app.services import data_import as data_import_service
from app.workers import data_import as worker
from tests.customer_helpers import TEST_DATABASE_URL, make_user
from tests.test_data_import_service import (
    DATE_ONE,
    _cleanup,
    _CommittedState,
    _context,
    _enqueue,
    _make_definition,
    _row,
    _store_json,
)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def committed(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_CommittedState]:
    state = _CommittedState(factory)
    monkeypatch.setattr(data_import_service, "session_factory", factory)
    monkeypatch.setattr(audit_service, "session_factory", factory)
    monkeypatch.setattr(worker, "session_factory", factory)
    try:
        yield state
    finally:
        await _cleanup(state)


async def test_worker_claims_job_uses_live_owner_and_honors_stop_event(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        table = await _make_definition(setup, committed, owner)
        context = _context(owner)
        job = await _enqueue(
            setup,
            table,
            _store_json([_row(DATE_ONE, 1, 1)]),
            context,
        )
        job_id = job.id
        await setup.commit()

    stop_event = asyncio.Event()
    invoked: list[tuple[int, str]] = []

    async def process(job_id: int, *, worker_id: str) -> None:
        async with factory() as session:
            claimed = await data_import_repository.get_import_job(session, job_id)
            assert claimed is not None
            assert claimed.status.value == "VALIDATING"
            assert claimed.lease_owner == worker_id
            assert claimed.lease_expires_at is not None
        invoked.append((job_id, worker_id))
        stop_event.set()

    monkeypatch.setattr(worker, "process_import_job", process)
    await asyncio.wait_for(
        worker.run_worker(poll_interval=0.01, stop_event=stop_event),
        timeout=2,
    )

    assert [item[0] for item in invoked] == [job_id]
    assert invoked[0][1]
    assert len(invoked[0][1]) <= 128


async def test_worker_loop_continues_after_one_job_raises(
    factory: async_sessionmaker[AsyncSession],
    committed: _CommittedState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    async with factory() as setup:
        owner = await make_user(setup)
        committed.track_user(owner)
        context = _context(owner)
        job_ids: list[int] = []
        for _ in range(2):
            table = await _make_definition(setup, committed, owner)
            job = await _enqueue(
                setup,
                table,
                _store_json([_row(DATE_ONE, 1, 1)]),
                context,
            )
            job_ids.append(job.id)
        await setup.commit()

    stop_event = asyncio.Event()
    invoked: list[int] = []

    async def process(job_id: int, *, worker_id: str) -> None:
        del worker_id
        invoked.append(job_id)
        if len(invoked) == 1:
            raise RuntimeError("one import-specific failure")
        stop_event.set()

    monkeypatch.setattr(worker, "process_import_job", process)
    await asyncio.wait_for(
        worker.run_worker(poll_interval=0.01, stop_event=stop_event),
        timeout=2,
    )

    assert invoked == job_ids


@pytest.mark.parametrize("poll_interval", [0, -0.1])
async def test_run_worker_rejects_nonpositive_poll_interval(
    poll_interval: float,
) -> None:
    with pytest.raises(ValueError, match="poll_interval must be positive"):
        await worker.run_worker(poll_interval=poll_interval)


def test_entrypoint_accepts_configurable_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    async def run_worker(
        *,
        poll_interval: float,
        stop_event: asyncio.Event | None,
    ) -> None:
        observed["poll_interval"] = poll_interval
        assert stop_event is not None
        stop_event.set()

    monkeypatch.setattr(worker, "run_worker", run_worker)
    worker.main(["--poll-interval", "0.25"])
    assert observed == {"poll_interval": 0.25}
