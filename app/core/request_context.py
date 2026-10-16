import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import uuid4

USER_AGENT_MAX_LENGTH = 512
REQUEST_ID_MAX_LENGTH = 64
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def normalize_request_id(value: str | None) -> str:
    """Return a safe request id, generating a UUID for missing or invalid input."""
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value) is not None:
        return value
    return str(uuid4())


def bounded_request_id(value: str | None) -> str:
    """Bound a request id to the storage column width."""
    return (value or "")[:REQUEST_ID_MAX_LENGTH]


@dataclass(frozen=True)
class RequestMeta:
    request_id: str
    method: str
    path: str
    ip: str | None
    user_agent: str | None


_request_meta_context: ContextVar[RequestMeta | None] = ContextVar(
    "request_meta",
    default=None,
)


def current_request_meta() -> RequestMeta | None:
    return _request_meta_context.get()


def set_request_meta(meta: RequestMeta) -> Token[RequestMeta | None]:
    return _request_meta_context.set(meta)


def reset_request_meta(token: Token[RequestMeta | None]) -> None:
    _request_meta_context.reset(token)


__all__: tuple[str, ...] = (
    "REQUEST_ID_MAX_LENGTH",
    "USER_AGENT_MAX_LENGTH",
    "RequestMeta",
    "bounded_request_id",
    "current_request_meta",
    "normalize_request_id",
    "reset_request_meta",
    "set_request_meta",
)
