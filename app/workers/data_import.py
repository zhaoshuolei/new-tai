"""Durable data import worker loop and module entrypoint."""

import argparse
import asyncio
import logging
import os
import signal
import socket
from collections.abc import Sequence
from uuid import uuid4

from app.db.session import session_factory
from app.repositories import data_import as data_import_repository
from app.services.data_import import process_import_job

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 1.0


def _worker_id() -> str:
    host = socket.gethostname()[:40]
    return f"{host}-{os.getpid()}-{uuid4().hex}"


async def run_worker(
    *,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Claim and process imports until shutdown is requested.

    Each job is isolated: an exception from one task is logged without
    terminating the loop, and the next poll continues from the durable queue.
    """
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    stopping = stop_event or asyncio.Event()
    worker_id = _worker_id()

    while not stopping.is_set():
        try:
            async with session_factory() as session:
                job = await data_import_repository.claim_next_import_job(
                    session,
                    worker_id=worker_id,
                )
        except Exception:
            logger.warning("Could not claim a data import job")
            job = None

        if job is not None:
            try:
                await process_import_job(job.id, worker_id=worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Data import job %s failed in worker", job.id)
            continue

        try:
            await asyncio.wait_for(stopping.wait(), timeout=poll_interval)
        except TimeoutError:
            continue


async def _run_with_signal_handlers(poll_interval: float) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError, RuntimeError:
            pass
    await run_worker(poll_interval=poll_interval, stop_event=stop_event)


def _poll_interval_from_env() -> float:
    raw = os.getenv("DATA_IMPORT_POLL_INTERVAL", str(_DEFAULT_POLL_INTERVAL))
    try:
        return float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "DATA_IMPORT_POLL_INTERVAL must be a number"
        ) from error


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the durable data import worker")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=_poll_interval_from_env(),
        help="Seconds to wait between empty queue polls",
    )
    args = parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_with_signal_handlers(args.poll_interval))


if __name__ == "__main__":
    main()


__all__: tuple[str, ...] = ("main", "run_worker")
