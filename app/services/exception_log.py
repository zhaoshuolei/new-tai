"""异常日志写入与指纹计算。"""

import hashlib
import re
import traceback
from datetime import UTC, datetime

from app.core.best_effort import run_best_effort
from app.core.logging import current_request_id, mask_sensitive_text
from app.core.request_context import RequestMeta, bounded_request_id
from app.db.session import session_factory
from app.models.exception_log import ExceptionLevel, ExceptionLog
from app.repositories import exception_log as exception_log_repository

MESSAGE_MAX_LENGTH = 1024

_DIGIT_RUN_PATTERN: re.Pattern[str] = re.compile(r"\d+")
_STACK_FRAME_PATTERN: re.Pattern[str] = re.compile(r"^\s*File .*$", re.MULTILINE)


def _normalize_message(message: str) -> str:
    return _DIGIT_RUN_PATTERN.sub("N", message)


def _top_stack_frame(traceback_text: str) -> str:
    frames = list(_STACK_FRAME_PATTERN.finditer(traceback_text))
    if not frames:
        return ""
    # The final frame in a Python traceback is the innermost call.
    return frames[-1].group(0).strip()


def build_fingerprint(
    *,
    exception_type: str,
    message: str,
    traceback_text: str,
) -> str:
    """Group exceptions by type, normalized message, and top stack frame."""
    payload = "\n".join(
        (
            exception_type,
            _normalize_message(message),
            _top_stack_frame(traceback_text),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]


def _format_traceback(exc: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__),
    )


async def record_exception(
    *,
    exc: BaseException,
    level: ExceptionLevel,
    request_meta: RequestMeta | None,
    user_id: int | None,
    status_code: int | None,
) -> None:
    """Persist one exception row in an independent best-effort transaction."""
    exception_type = type(exc).__name__
    message = mask_sensitive_text(str(exc))[:MESSAGE_MAX_LENGTH]
    traceback_text = mask_sensitive_text(_format_traceback(exc))
    fingerprint = build_fingerprint(
        exception_type=exception_type,
        message=message,
        traceback_text=traceback_text,
    )
    occurred_at = datetime.now(UTC).replace(tzinfo=None)
    request_id = bounded_request_id(
        request_meta.request_id if request_meta is not None else current_request_id()
    )

    async def write_entry() -> None:
        row = ExceptionLog(
            exception_type=exception_type,
            message=message,
            fingerprint=fingerprint,
            level=level,
            request_id=request_id,
            method=request_meta.method if request_meta is not None else "",
            path=request_meta.path if request_meta is not None else "",
            user_id=user_id,
            status_code=status_code,
            occurred_at=occurred_at,
        )
        async with session_factory() as session:
            await exception_log_repository.add(session, row)
            await session.commit()

    await run_best_effort(
        write_entry,
        description=f"record {exception_type} exception log",
    )


__all__: tuple[str, ...] = (
    "MESSAGE_MAX_LENGTH",
    "build_fingerprint",
    "record_exception",
)
