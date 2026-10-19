"""Receive-boundary body limit middleware tests."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest
from starlette.types import Message, Receive, Scope, Send

from app.core.middleware import ReceiveBodyLimitMiddleware, ReceiveBodyLimitRule


class _LimitReached(Exception):
    pass


def _scope(*, content_type: str | None = None) -> Scope:
    headers = (
        []
        if content_type is None
        else [(b"content-type", content_type.encode("ascii"))]
    )
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/data-tables/1/imports/excel",
        "headers": headers,
    }


def _rule(*, max_bytes: int, multipart_overhead_bytes: int = 0) -> ReceiveBodyLimitRule:
    return ReceiveBodyLimitRule(
        path_pattern=re.compile(r"(?:.*/)?data-tables/[^/]+/imports/excel"),
        method="POST",
        max_body_bytes=lambda: max_bytes,
        multipart_overhead_bytes=multipart_overhead_bytes,
        error_factory=_LimitReached,
    )


def _receive(messages: Iterable[Message]) -> Receive:
    iterator = iter(messages)

    async def receive() -> Message:
        return next(iterator)

    return receive


async def _send(message: Message) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, "3"])
async def test_cumulative_actual_bytes_ignore_missing_or_lying_content_length(
    declared_length: str | None,
) -> None:
    scope = _scope()
    if declared_length is not None:
        scope["headers"] = [(b"content-length", declared_length.encode("ascii"))]

    async def app(
        request_scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if not message.get("more_body", False):
                return

    middleware = ReceiveBodyLimitMiddleware(
        app,
        rules=(_rule(max_bytes=8),),
    )
    receive = _receive(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56789", "more_body": False},
        ]
    )

    with pytest.raises(_LimitReached):
        await middleware(scope, receive, _send)


@pytest.mark.asyncio
async def test_exact_boundary_is_returned_and_disconnect_is_not_counted() -> None:
    observed: list[Message] = []

    async def app(
        request_scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        while True:
            message = await receive()
            observed.append(message)
            if message["type"] == "http.disconnect":
                return

    middleware = ReceiveBodyLimitMiddleware(
        app,
        rules=(_rule(max_bytes=8),),
    )
    receive = _receive(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    await middleware(_scope(), receive, _send)

    assert [message["type"] for message in observed] == [
        "http.request",
        "http.request",
        "http.disconnect",
    ]
    assert (
        b"".join(
            message.get("body", b"")
            for message in observed
            if message["type"] == "http.request"
        )
        == b"12345678"
    )


@pytest.mark.asyncio
async def test_multipart_rule_allows_exactly_64_kib_of_raw_overhead() -> None:
    max_file_bytes = 8
    multipart_overhead_bytes = 64 * 1024
    received = bytearray()

    async def app(
        request_scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            received.extend(message.get("body", b""))
            if not message.get("more_body", False):
                return

    middleware = ReceiveBodyLimitMiddleware(
        app,
        rules=(
            _rule(
                max_bytes=max_file_bytes,
                multipart_overhead_bytes=multipart_overhead_bytes,
            ),
        ),
    )
    exact_body = b"x" * (max_file_bytes + multipart_overhead_bytes)
    receive = _receive(
        [
            {
                "type": "http.request",
                "body": exact_body[:1000],
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": exact_body[1000:],
                "more_body": False,
            },
        ]
    )

    await middleware(
        _scope(content_type="multipart/form-data; boundary=test"),
        receive,
        _send,
    )

    assert bytes(received) == exact_body
