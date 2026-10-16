import copy
import json
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Final, TextIO

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

_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(\bauthorization\b\s*[:=]\s*)([^\r\n,}\]]+)",
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(\b(?:password|access_token|refresh_token|token|code)\b"
    r"\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\r\n,}\]]+)",
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
    masked = _AUTHORIZATION_PATTERN.sub(
        lambda match: f"{match.group(1)}{MASK}",
        value,
    )
    return _SENSITIVE_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{MASK}",
        masked,
    )


def mask_sensitive_data(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): (
                MASK
                if str(key).casefold() in SENSITIVE_KEYS
                else mask_sensitive_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_sensitive_data(item) for item in value)
    if isinstance(value, set):
        return [mask_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return mask_sensitive_text(value)
    if isinstance(value, BaseException):
        return mask_sensitive_text(str(value))
    return value


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
            masked_fields = mask_sensitive_data(extra_fields)
            if isinstance(masked_fields, dict):
                for key, value in masked_fields.items():
                    payload.setdefault(key, value)

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRIBUTES or key in payload:
                continue
            if key in {"extra_fields", "request_id"} or key.startswith("_"):
                continue
            payload[key] = mask_sensitive_data(value)

        if record.exc_info:
            payload["exception"] = mask_sensitive_text(
                self.formatException(record.exc_info),
            )
        elif record.exc_text:
            payload["exception"] = mask_sensitive_text(record.exc_text)

        return json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
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
            masked_fields = mask_sensitive_data(extra_fields)
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
    "configure_logging",
    "current_request_id",
    "mask_sensitive_data",
    "mask_sensitive_text",
    "reset_request_id",
    "set_request_id",
)
