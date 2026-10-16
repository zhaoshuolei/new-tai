from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.errors import BusinessError, ErrorCode
from app.db.session import session_factory
from app.main import create_app
from app.models.exception_log import ExceptionLevel, ExceptionLog
from app.repositories import exception_log as exception_log_repository
from app.services.exception_log import build_fingerprint

REQUEST_PREFIX = "test-exception-log-"


def unique_request_id() -> str:
    return f"{REQUEST_PREFIX}{uuid4().hex}"


@pytest_asyncio.fixture(autouse=True)
async def _clean_exception_rows() -> AsyncIterator[None]:
    async def clear_rows() -> None:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(
                delete(ExceptionLog).where(
                    ExceptionLog.request_id.like(f"{REQUEST_PREFIX}%"),
                )
            )
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield
    finally:
        await clear_rows()


async def exception_rows(request_id: str) -> list[ExceptionLog]:
    async with session_factory() as lookup_session:
        statement = (
            select(ExceptionLog)
            .where(ExceptionLog.request_id == request_id)
            .order_by(ExceptionLog.id)
        )
        return list((await lookup_session.scalars(statement)).all())


def make_app():
    return create_app()


async def test_unhandled_exception_is_recorded() -> None:
    app = make_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom 123")

    request_id = unique_request_id()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/boom",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 500
    rows = await exception_rows(request_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.exception_type == "RuntimeError"
    assert "boom" in row.message
    assert row.level is ExceptionLevel.ERROR
    assert row.status_code == 500
    assert row.method == "GET"
    assert row.path == "/boom"
    assert row.occurred_at is not None
    assert row.acknowledged is False


async def test_exception_records_authenticated_user_id() -> None:
    app = make_app()

    @app.get("/boom-with-user")
    async def boom_with_user(request: Request) -> None:
        request.state.user_id = 99
        raise RuntimeError("boom")

    request_id = unique_request_id()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/boom-with-user",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 500
    rows = await exception_rows(request_id)
    assert len(rows) == 1
    assert rows[0].user_id == 99


async def test_exception_message_masked() -> None:
    app = make_app()

    @app.get("/leak")
    async def leak() -> None:
        raise RuntimeError("password=secret123")

    request_id = unique_request_id()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/leak",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 500
    rows = await exception_rows(request_id)
    assert len(rows) == 1
    assert "secret123" not in rows[0].message
    assert "***" in rows[0].message


async def test_http_exception_5xx_is_recorded() -> None:
    app = make_app()

    @app.get("/http-503")
    async def http_503() -> None:
        raise HTTPException(status_code=503, detail="service unavailable")

    request_id = unique_request_id()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/http-503",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 503
    rows = await exception_rows(request_id)
    assert len(rows) == 1
    assert rows[0].exception_type == "HTTPException"
    assert rows[0].status_code == 503


async def test_business_error_5xx_is_recorded() -> None:
    app = make_app()

    @app.get("/business-502")
    async def business_502() -> None:
        raise BusinessError(
            ErrorCode.FEISHU_UPSTREAM_ERROR,
            "upstream failed",
            http_status=502,
        )

    request_id = unique_request_id()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/business-502",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 502
    rows = await exception_rows(request_id)
    assert len(rows) == 1
    assert rows[0].exception_type == "BusinessError"
    assert rows[0].status_code == 502


async def test_business_error_4xx_is_not_recorded() -> None:
    app = make_app()

    @app.get("/business-404")
    async def business_404() -> None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    request_id = unique_request_id()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/business-404",
            headers={"X-Request-Id": request_id},
        )

    assert response.status_code == 404
    assert await exception_rows(request_id) == []


def test_build_fingerprint_normalizes_digits() -> None:
    first = build_fingerprint(
        exception_type="RuntimeError",
        message="boom 123",
        traceback_text="",
    )
    second = build_fingerprint(
        exception_type="RuntimeError",
        message="boom 456",
        traceback_text="",
    )

    assert first == second
    assert len(first) == 64


async def test_summary_and_ack_roundtrip() -> None:
    request_id = unique_request_id()
    fingerprint = build_fingerprint(
        exception_type="RuntimeError",
        message="boom",
        traceback_text="",
    )
    async with session_factory() as session:
        for acknowledged in (False, True):
            session.add(
                ExceptionLog(
                    exception_type="RuntimeError",
                    message="boom",
                    fingerprint=fingerprint,
                    level=ExceptionLevel.ERROR,
                    request_id=request_id,
                    method="GET",
                    path="/boom",
                    user_id=None,
                    status_code=500,
                    occurred_at=datetime.now(UTC).replace(tzinfo=None),
                    acknowledged=acknowledged,
                )
            )
        await session.commit()

    async with session_factory() as session:
        rows, total = await exception_log_repository.list_exceptions(
            session,
            page=1,
            page_size=20,
            exception_type="RuntimeError",
            level=ExceptionLevel.ERROR,
            acknowledged=None,
            start_time=None,
            end_time=None,
        )
        matching = [row for row in rows if row.request_id == request_id]
        assert total >= 1
        assert len(matching) == 2

        summaries = await exception_log_repository.summary_by_fingerprint(
            session,
            start_time=None,
            end_time=None,
        )
        summary = next(item for item in summaries if item.fingerprint == fingerprint)
        assert summary.count == 2
        assert not summary.acknowledged
        assert summary.latest_occurred_at is not None

        ack_at = datetime.now(UTC).replace(tzinfo=None)
        for matched in matching:
            row = await exception_log_repository.get_by_id(session, matched.id)
            assert row is not None
            await exception_log_repository.ack(
                session,
                row,
                user_id=42,
                at=ack_at,
            )
        await session.commit()

    async with session_factory() as session:
        summaries = await exception_log_repository.summary_by_fingerprint(
            session,
            start_time=None,
            end_time=None,
        )
        summary = next(item for item in summaries if item.fingerprint == fingerprint)
        assert summary.acknowledged
