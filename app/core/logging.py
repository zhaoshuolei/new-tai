import copy
import dataclasses
import json
import logging
import re
import traceback
from collections.abc import Callable, Mapping, Sequence, Set
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any, Final, TextIO, cast

SENSITIVE_KEYS = {
    "password",
    "token",
    "access_token",
    "refresh_token",
    "code",
    "authorization",
}
MASK = "***"

_request_id_context: ContextVar[str | None] = ContextVar(
    "request_id",
    default=None,
)

_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?P<key_quote>[\"']?)\b"
    r"(?P<key>access_token|refresh_token|authorization|password|token|code)"
    r"(?P=key_quote)\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\\\])*\"|'(?:\\.|[^'\\\\])*'"
    r"|[^,\r\n}\]]+)",
)
_STANDARD_RECORD_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__,
) | {"asctime", "message"}


def current_request_id() -> str:
    return _request_id_context.get() or ""


def set_request_id(request_id: str) -> Token[str | None]:
    return _request_id_context.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id_context.reset(token)


def mask_sensitive_text(value: str) -> str:
    return _SENSITIVE_VALUE_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{MASK}",
        value,
    )


def _mapping_key(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return _safe_object_text(value)


def _safe_object_text(value: object) -> str:
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return "<unprintable>"


def _model_data(value: object) -> object | None:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return cast(object, model_dump())
        except Exception:
            return None

    legacy_dump = getattr(value, "dict", None)
    if callable(legacy_dump):
        try:
            return cast(object, legacy_dump())
        except Exception:
            return None

    return None


def sanitize_log_value(
    value: object,
    *,
    _seen: set[int] | None = None,
) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_sensitive_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return mask_sensitive_text(
            bytes(value).decode("utf-8", errors="replace"),
        )
    if isinstance(value, BaseException):
        return mask_sensitive_text(_safe_object_text(value))

    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return "[recursive]"

    _seen.add(value_id)
    try:
        if isinstance(value, Mapping):
            sanitized: dict[str, object] = {}
            for key, item in value.items():
                key_text = mask_sensitive_text(_mapping_key(key))
                sanitized[key_text] = (
                    MASK
                    if key_text.casefold() in SENSITIVE_KEYS
                    else sanitize_log_value(item, _seen=_seen)
                )
            return sanitized

        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return sanitize_log_value(
                {
                    field.name: getattr(value, field.name)
                    for field in dataclasses.fields(value)
                },
                _seen=_seen,
            )

        dumped_model = _model_data(value)
        if dumped_model is not None:
            return sanitize_log_value(dumped_model, _seen=_seen)

        if isinstance(value, Sequence):
            return [sanitize_log_value(item, _seen=_seen) for item in value]

        if isinstance(value, Set):
            return [sanitize_log_value(item, _seen=_seen) for item in value]

        return mask_sensitive_text(_safe_object_text(value))
    finally:
        _seen.discard(value_id)


def mask_sensitive_data(value: object) -> object:
    return sanitize_log_value(value)


def _sanitize_record_args(value: object) -> object:
    if isinstance(value, tuple):
        return tuple(sanitize_log_value(item) for item in value)
    return sanitize_log_value(value)


def sanitize_log_record(record: logging.LogRecord) -> None:
    record.args = cast(Any, _sanitize_record_args(record.args))
    try:
        message = record.getMessage()
    except Exception:
        message = _safe_object_text(record.msg)
    record.msg = mask_sensitive_text(message)
    record.args = ()

    for key, value in list(record.__dict__.items()):
        if key in _STANDARD_RECORD_ATTRIBUTES:
            continue
        record.__dict__[key] = sanitize_log_value(value)

    if record.exc_info:
        record.exc_text = mask_sensitive_text(
            "".join(traceback.format_exception(*record.exc_info)),
        )
    elif record.exc_text:
        record.exc_text = mask_sensitive_text(record.exc_text)


class SanitizingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        sanitize_log_record(record)
        return True


def _registered_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    seen: set[int] = set()
    loggers = [logging.getLogger()]
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            loggers.append(logger)

    for logger in loggers:
        for handler in logger.handlers:
            handler_id = id(handler)
            if handler_id in seen:
                continue
            seen.add(handler_id)
            handlers.append(handler)
    return handlers


def _install_sanitizing_filters() -> None:
    for handler in _registered_handlers():
        if any(
            isinstance(log_filter, SanitizingFilter) for log_filter in handler.filters
        ):
            continue
        handler.addFilter(SanitizingFilter())


class SanitizingLogRecordFactory:
    def __init__(
        self,
        base_factory: Callable[..., logging.LogRecord],
    ) -> None:
        self._base_factory = base_factory

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = self._base_factory(*args, **kwargs)
        sanitize_log_record(record)
        return record


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", None) or current_request_id()
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_sensitive_text(record.getMessage()),
        }
        if request_id:
            payload["request_id"] = request_id

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, Mapping):
            masked_fields = sanitize_log_value(extra_fields)
            if isinstance(masked_fields, dict):
                for key, value in masked_fields.items():
                    payload.setdefault(key, value)

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRIBUTES or key in payload:
                continue
            if key in {"extra_fields", "request_id"} or key.startswith("_"):
                continue
            payload[key] = sanitize_log_value(value)

        if record.exc_text:
            payload["exception"] = mask_sensitive_text(record.exc_text)
        elif record.exc_info:
            payload["exception"] = mask_sensitive_text(
                self.formatException(record.exc_info),
            )

        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )


class LocalFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
        )

    def format(self, record: logging.LogRecord) -> str:
        formatted_record = copy.copy(record)
        formatted_record.request_id = (
            getattr(record, "request_id", None) or current_request_id() or "-"
        )
        message = mask_sensitive_text(record.getMessage())

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, Mapping):
            masked_fields = sanitize_log_value(extra_fields)
            if isinstance(masked_fields, dict) and masked_fields:
                rendered_fields = " ".join(
                    f"{key}={value!r}" for key, value in masked_fields.items()
                )
                message = f"{message} {rendered_fields}"

        formatted_record.msg = message
        formatted_record.args = ()
        if formatted_record.exc_info:
            formatted_record.exc_text = mask_sensitive_text(
                self.formatException(formatted_record.exc_info),
            )
        elif formatted_record.exc_text:
            formatted_record.exc_text = mask_sensitive_text(
                formatted_record.exc_text,
            )

        return super().format(formatted_record)


class _ManagedStreamHandler(logging.StreamHandler[TextIO]):
    pass


def configure_logging(env: str) -> None:
    current_factory = logging.getLogRecordFactory()
    if not isinstance(current_factory, SanitizingLogRecordFactory):
        logging.setLogRecordFactory(
            SanitizingLogRecordFactory(current_factory),
        )

    _install_sanitizing_filters()
    root_logger = logging.getLogger()
    formatter: logging.Formatter
    if env == "local":
        formatter = LocalFormatter()
    else:
        formatter = JsonFormatter()

    managed_handler = next(
        (
            handler
            for handler in root_logger.handlers
            if isinstance(handler, _ManagedStreamHandler)
        ),
        None,
    )
    if managed_handler is None:
        managed_handler = _ManagedStreamHandler()
        root_logger.addHandler(managed_handler)

    managed_handler.setFormatter(formatter)
    if not any(
        isinstance(log_filter, SanitizingFilter)
        for log_filter in managed_handler.filters
    ):
        managed_handler.addFilter(SanitizingFilter())
    if not any(
        isinstance(log_filter, RequestContextFilter)
        for log_filter in managed_handler.filters
    ):
        managed_handler.addFilter(RequestContextFilter())
    root_logger.setLevel(logging.INFO)


__all__: tuple[str, ...] = (
    "JsonFormatter",
    "LocalFormatter",
    "MASK",
    "RequestContextFilter",
    "SENSITIVE_KEYS",
    "SanitizingFilter",
    "SanitizingLogRecordFactory",
    "configure_logging",
    "current_request_id",
    "mask_sensitive_data",
    "mask_sensitive_text",
    "reset_request_id",
    "sanitize_log_record",
    "sanitize_log_value",
    "set_request_id",
)
