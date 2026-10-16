"""Best-effort execution for non-critical side effects."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


async def run_best_effort(
    factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    timeout: float = 2.0,
    description: str,
) -> None:
    """Run an async factory without allowing failure to affect the caller."""
    try:
        await asyncio.wait_for(factory(), timeout=timeout)
    except Exception:
        logger.warning("%s failed", description, exc_info=True)


__all__: tuple[str, ...] = ("run_best_effort",)
