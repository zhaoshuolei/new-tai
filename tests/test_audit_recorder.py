import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth_context import AuthContext
from app.core.best_effort import run_best_effort
from app.core.errors import BusinessError, ErrorCode
from app.core.request_context import (
    RequestMeta,
    reset_request_meta,
    set_request_meta,
)
from app.models.audit_log import AuditLog, AuditResult
from app.models.role import DataScope
from app.models.user import User
from app.repositories.audit_log import list_audit_logs
from app.services import audit as audit_service

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)

SUPER_ADMIN_CONTEXT = AuthContext(
    user_id=1,
    role_id=1,
    department_id=None,
    data_scope=DataScope.ALL,
    custom_department_ids=frozenset(),
    include_children=True,
    permission_codes=frozenset(),
    is_super_admin=True,
    username="admin",
    nickname="超级管理员",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def isolated_audit_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_service, "session_factory", factory)

    async def clear_rows() -> None:
        async with factory() as cleanup_session:
            await cleanup_session.execute(delete(AuditLog))
            await cleanup_session.commit()

    await clear_rows()
    try:
        yield
    finally:
        await clear_rows()
        await engine.dispose()


async def test_success_audit_written(session: AsyncSession) -> None:
    @audit_service.audited(
        action="user:update",
        resource_type="user",
        resource_id_param="user_id",
    )
    async def op(
        session: AsyncSession,
        *,
        context: AuthContext,
        user_id: int,
    ) -> str:
        return "ok"

    meta = RequestMeta(
        request_id="req-success",
        method="PATCH",
        path="/api/v1/users/7",
        ip="127.0.0.1",
        user_agent="pytest",
    )
    token = set_request_meta(meta)
    try:
        assert await op(session, context=SUPER_ADMIN_CONTEXT, user_id=7) == "ok"
    finally:
        reset_request_meta(token)

    row = (await session.scalars(select(AuditLog))).one()
    assert row.result is AuditResult.SUCCESS
    assert row.resource_id == 7
    assert row.user_id == SUPER_ADMIN_CONTEXT.user_id
    assert row.actor_username == "admin"
    assert row.actor_nickname == "超级管理员"
    assert row.request_id == "req-success"
    assert row.method == "PATCH"
    assert row.path == "/api/v1/users/7"
    assert row.ip == "127.0.0.1"


async def test_denied_audit_survives_rollback(session: AsyncSession) -> None:
    @audit_service.audited(
        action="user:update",
        resource_type="user",
        resource_id_param="user_id",
    )
    async def op(
        session: AsyncSession,
        *,
        context: AuthContext,
        user_id: int,
    ) -> None:
        session.add(User(username="ghost"))
        raise BusinessError(
            ErrorCode.LAST_SUPER_ADMIN,
            "last super admin",
            http_status=403,
        )

    with pytest.raises(BusinessError) as error_info:
        await op(session, context=SUPER_ADMIN_CONTEXT, user_id=7)

    await session.rollback()
    row = (await session.scalars(select(AuditLog))).one()
    assert row.result is AuditResult.DENIED
    assert row.reason == "LAST_SUPER_ADMIN"
    assert row.resource_id == 7
    assert getattr(error_info.value, "audit_recorded", False) is True


async def test_non_denial_not_recorded(session: AsyncSession) -> None:
    @audit_service.audited(
        action="user:update",
        resource_type="user",
        resource_id_param="user_id",
    )
    async def op(
        session: AsyncSession,
        *,
        context: AuthContext,
        user_id: int,
    ) -> None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "user not found",
            http_status=404,
        )

    with pytest.raises(BusinessError) as error_info:
        await op(session, context=SUPER_ADMIN_CONTEXT, user_id=7)

    assert (await session.scalars(select(AuditLog))).all() == []
    assert getattr(error_info.value, "audit_recorded", False) is False


async def test_missing_context_writes_null_actor_and_uses_result_attr(
    session: AsyncSession,
) -> None:
    @audit_service.audited(
        action="user:create",
        resource_type="user",
        resource_id_attr="id",
    )
    async def op(session: AsyncSession) -> SimpleNamespace:
        return SimpleNamespace(id=42)

    assert await op(session) == SimpleNamespace(id=42)

    row = (await session.scalars(select(AuditLog))).one()
    assert row.user_id is None
    assert row.actor_username is None
    assert row.actor_nickname is None
    assert row.resource_id == 42
    assert row.request_id == ""


async def test_write_failure_does_not_propagate(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_session_factory() -> None:
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(audit_service, "session_factory", broken_session_factory)

    @audit_service.audited(
        action="user:update",
        resource_type="user",
        resource_id_param="user_id",
    )
    async def op(
        session: AsyncSession,
        *,
        context: AuthContext,
        user_id: int,
    ) -> str:
        return "ok"

    assert await op(session, context=SUPER_ADMIN_CONTEXT, user_id=7) == "ok"


async def test_denied_audit_write_failure_preserves_original_error(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_session_factory() -> None:
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(audit_service, "session_factory", broken_session_factory)

    @audit_service.audited(
        action="user:update",
        resource_type="user",
        resource_id_param="user_id",
    )
    async def op(
        session: AsyncSession,
        *,
        context: AuthContext,
        user_id: int,
    ) -> None:
        raise BusinessError(
            ErrorCode.CANNOT_MODIFY_SELF,
            "cannot modify self",
            http_status=403,
        )

    with pytest.raises(BusinessError) as error_info:
        await op(session, context=SUPER_ADMIN_CONTEXT, user_id=7)

    assert error_info.value.code == ErrorCode.CANNOT_MODIFY_SELF
    assert getattr(error_info.value, "audit_recorded", False) is True


async def test_run_best_effort_swallows_failures_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail() -> None:
        raise RuntimeError("audit unavailable")

    async def slow() -> None:
        await asyncio.sleep(0.1)

    with caplog.at_level(logging.WARNING, logger="app.core.best_effort"):
        await run_best_effort(
            fail,
            timeout=0.1,
            description="audit failure test",
        )
        await run_best_effort(
            slow,
            timeout=0.001,
            description="audit timeout test",
        )

    assert "audit failure test" in caplog.text
    assert "audit timeout test" in caplog.text


def test_is_denial_matches_status_and_security_codes() -> None:
    assert audit_service.is_denial(
        BusinessError(ErrorCode.INVALID_PARAMETER, "forbidden", http_status=403)
    )
    for code in audit_service.DENIED_ERROR_CODES:
        assert audit_service.is_denial(BusinessError(code, "denied"))
    assert not audit_service.is_denial(
        BusinessError(ErrorCode.USER_NOT_FOUND, "missing", http_status=404)
    )


async def test_list_audit_logs_filters_and_paginates(session: AsyncSession) -> None:
    created_at = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        AuditLog(
            user_id=1,
            actor_username="alice",
            actor_nickname="管理员",
            action="user:update",
            resource_type="user",
            resource_id=7,
            result=AuditResult.SUCCESS,
            reason=None,
            method="PATCH",
            path="/api/v1/users/7",
            ip="127.0.0.1",
            request_id="r1",
            created_at=created_at,
        ),
        AuditLog(
            user_id=1,
            actor_username="alice",
            actor_nickname="管理员",
            action="user:delete",
            resource_type="user",
            resource_id=8,
            result=AuditResult.DENIED,
            reason="FORBIDDEN",
            method="DELETE",
            path="/api/v1/users/8",
            ip="127.0.0.1",
            request_id="r2",
            created_at=created_at + timedelta(seconds=1),
        ),
        AuditLog(
            user_id=2,
            actor_username="bob",
            actor_nickname="操作员",
            action="role:update",
            resource_type="role",
            resource_id=3,
            result=AuditResult.SUCCESS,
            reason=None,
            method="PATCH",
            path="/api/v1/roles/3",
            ip="127.0.0.2",
            request_id="r3",
            created_at=created_at + timedelta(seconds=2),
        ),
    ]
    session.add_all(rows)
    await session.commit()

    filtered, total = await list_audit_logs(
        session,
        page=1,
        page_size=1,
        user_id=1,
        action=None,
        resource_type="user",
        resource_id=None,
        result=None,
        start_time=created_at,
        end_time=created_at,
        keyword="管理",
    )
    assert total == 1
    assert [row.request_id for row in filtered] == ["r1"]

    latest, latest_total = await list_audit_logs(
        session,
        page=1,
        page_size=10,
        user_id=None,
        action=None,
        resource_type=None,
        resource_id=None,
        result=None,
        start_time=None,
        end_time=None,
        keyword=None,
    )
    assert latest_total == 3
    assert [row.request_id for row in latest] == ["r3", "r2", "r1"]
