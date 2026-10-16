import logging
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.logging import configure_logging, mask_sensitive_text


@dataclass
class _DataclassSecret:
    password: str


class _ModelSecret(BaseModel):
    password: str
    nested: dict[str, Any]


class _UnknownSecret:
    def __init__(self, password: str) -> None:
        self.password = password

    def __repr__(self) -> str:
        return f"_UnknownSecret(password={self.password!r})"


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.output = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.output = self.format(record)


def _capture_with_existing_handler(
    logger_name: str,
    value: object,
) -> str:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler()
    handler.setFormatter(
        logging.Formatter("%(message)s %(extra_fields)s"),
    )
    logger.handlers = [handler]
    logger.propagate = False

    configure_logging("test")
    logger.info("payload", extra={"extra_fields": {"value": value}})
    return handler.output


def _capture_plain_message_with_existing_handler(
    logger_name: str,
    message: str,
) -> str:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.propagate = False

    configure_logging("test")
    logger.info(message)
    return handler.output


def _capture_message_with_existing_handler(
    logger_name: str,
    message: str,
    argument: object,
) -> str:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.propagate = False

    configure_logging("test")
    logger.info(message, argument)
    return handler.output


@pytest.mark.parametrize(
    "text",
    [
        '{"password":"secret-json"}',
        "{'password': 'secret-dict'}",
        '{"authorization": "secret-quoted"}',
    ],
)
def test_quoted_sensitive_keys_are_redacted_in_message_text(text: str) -> None:
    output = mask_sensitive_text(text)
    handler_output = _capture_plain_message_with_existing_handler(
        f"tests.redaction.message.{len(text)}",
        text,
    )

    assert "secret-" not in output
    assert "***" in output
    assert "secret-" not in handler_output
    assert "***" in handler_output


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        (b"password=secret-bytes", "secret-bytes"),
        (_DataclassSecret(password="secret-dataclass"), "secret-dataclass"),
        (
            _ModelSecret(
                password="secret-pydantic",
                nested={"access_token": "secret-nested"},
            ),
            "secret-nested",
        ),
        (
            {"outer": [{"inner": {"refresh_token": "secret-deep"}}]},
            "secret-deep",
        ),
        (_UnknownSecret(password="secret-object"), "secret-object"),
    ],
)
def test_redaction_is_applied_before_any_handler(
    value: object,
    secret: str,
) -> None:
    output = _capture_with_existing_handler(
        f"tests.redaction.{secret}",
        value,
    )

    assert secret not in output
    assert "***" in output


def test_formatted_secret_argument_is_redacted_before_handler() -> None:
    output = _capture_message_with_existing_handler(
        "tests.redaction.formatted",
        "password=%s",
        "secret-formatted",
    )

    assert "secret-formatted" not in output
    assert "password=***" in output


def test_nested_pydantic_model_never_reaches_handler_unredacted() -> None:
    value = _ModelSecret(
        password="secret-pydantic-password",
        nested={"payload": _DataclassSecret(password="secret-nested-dataclass")},
    )

    output = _capture_with_existing_handler(
        "tests.redaction.pydantic",
        value,
    )

    assert "secret-pydantic-password" not in output
    assert "secret-nested-dataclass" not in output
    assert "***" in output
