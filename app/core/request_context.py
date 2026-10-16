from contextvars import ContextVar, Token
from dataclasses import dataclass

USER_AGENT_MAX_LENGTH = 512


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
    "USER_AGENT_MAX_LENGTH",
    "RequestMeta",
    "current_request_meta",
    "reset_request_meta",
    "set_request_meta",
)
