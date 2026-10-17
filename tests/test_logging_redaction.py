import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import JsonFormatter, configure_logging, mask_sensitive_text
from app.db.session import engine


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


def test_api_key_is_masked() -> None:
    assert mask_sensitive_text("api_key=mcpk2_secret") == "api_key=***"


def test_api_key_is_masked_in_json_style() -> None:
    assert mask_sensitive_text('"api_key": "mcpk2_secret"') == '"api_key": "***"'


def test_app_secret_is_masked_in_structured_log_extra() -> None:
    output = _capture_with_existing_handler(
        "tests.redaction.app-secret-structured",
        {"app_secret": "secret-value"},
    )

    assert "secret-value" not in output
    assert "***" in output


def test_app_secret_is_masked_in_json_formatter() -> None:
    logger = logging.getLogger("tests.redaction.app-secret-json")
    handler = _CapturingHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.propagate = False

    configure_logging("test")
    logger.info(
        "payload",
        extra={"extra_fields": {"app_secret": "secret-value"}},
    )

    payload = json.loads(handler.output)
    assert "secret-value" not in handler.output
    assert payload["app_secret"] == "***"


def test_app_secret_is_masked_in_formatted_exception() -> None:
    logger = logging.getLogger("tests.redaction.app-secret-exception")
    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.propagate = False

    configure_logging("test")
    try:
        raise RuntimeError("request failed: app_secret=secret-value")
    except RuntimeError:
        logger.exception("request failed")

    assert "secret-value" not in handler.output
    assert "app_secret=***" in handler.output


@pytest.mark.parametrize(
    "text",
    [
        "app_secret=secret-value",
        '{"app_secret":"secret-value"}',
        'app_secret="secret-value"',
    ],
)
def test_app_secret_is_masked_in_message_text(text: str) -> None:
    output = mask_sensitive_text(text)

    assert "secret-value" not in output
    assert "***" in output


def test_formatted_app_secret_argument_is_redacted_before_handler() -> None:
    output = _capture_message_with_existing_handler(
        "tests.redaction.app-secret-formatted",
        "app_secret=%s",
        "secret-value",
    )

    assert "secret-value" not in output
    assert "app_secret=***" in output


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
    "text",
    [
        '{"password": "ab\\"c,d"}',
        '{"token": "a\\"b,c"}',
        'password="xy\\"z,w"',
    ],
)
def test_escaped_sensitive_values_are_fully_redacted(text: str) -> None:
    """转义引号后若正则提前失配，会退化成贪婪匹配并泄漏逗号后的密文尾部。"""
    output = mask_sensitive_text(text)

    assert "***" in output
    assert ",d" not in output
    assert ",c" not in output
    assert ",w" not in output


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


async def test_database_exception_hides_argon2_parameter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("test")
    argon2_hash = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$c29tZWhhc2g"

    with pytest.raises(SQLAlchemyError) as error:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT * FROM definitely_missing_table "
                    "WHERE password_hash = :password_hash"
                ),
                {"password_hash": argon2_hash},
            )

    logger = logging.getLogger("tests.redaction.sqlalchemy")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        logger.exception("database operation failed", exc_info=error.value)

    assert engine.sync_engine.hide_parameters is True
    assert "database operation failed" in caplog.text
    assert argon2_hash not in caplog.text
