"""系统监控接口。"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.permissions import require_permission
from app.core.redis import get_redis
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.exception_log import ExceptionLevel
from app.repositories import exception_log as exception_log_repository
from app.schemas.monitor import (
    CurrentSnapshotRead,
    ExceptionLogRead,
    ExceptionSummaryRead,
    SystemMetricRead,
)
from app.services import monitor as monitor_service

router = APIRouter(
    prefix="/monitor/system",
    tags=["系统监控"],
    dependencies=[Depends(require_permission("monitor:read"))],
)

exceptions_router = APIRouter(prefix="/monitor/exceptions", tags=["异常监控"])


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@router.get(
    "/current",
    response_model=SuccessEnvelope[CurrentSnapshotRead],
)
async def get_current_snapshot(
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    snapshot = await monitor_service.collect_live_snapshot(session, redis)
    return success(CurrentSnapshotRead.model_validate(snapshot))


@router.get(
    "/history",
    response_model=SuccessEnvelope[Page[SystemMetricRead]],
)
async def get_metric_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    metrics, total = await monitor_service.list_history(
        session,
        page=page,
        page_size=page_size,
        start_time=start_time,
        end_time=end_time,
    )
    return success(
        Page[SystemMetricRead](
            items=[SystemMetricRead.model_validate(metric) for metric in metrics],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@exceptions_router.get(
    "",
    response_model=SuccessEnvelope[Page[ExceptionLogRead]],
    dependencies=[Depends(require_permission("exception:read"))],
)
async def list_exceptions(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    exception_type: str | None = None,
    level: ExceptionLevel | None = None,
    acknowledged: bool | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    rows, total = await exception_log_repository.list_exceptions(
        session,
        page=page,
        page_size=page_size,
        exception_type=exception_type,
        level=level,
        acknowledged=acknowledged,
        start_time=start_time,
        end_time=end_time,
    )
    return success(
        Page[ExceptionLogRead](
            items=[ExceptionLogRead.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@exceptions_router.get(
    "/summary",
    response_model=SuccessEnvelope[list[ExceptionSummaryRead]],
    dependencies=[Depends(require_permission("exception:read"))],
)
async def summarize_exceptions(
    session: Annotated[AsyncSession, Depends(get_session)],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    summaries = await exception_log_repository.summary_by_fingerprint(
        session,
        start_time=start_time,
        end_time=end_time,
    )
    return success([ExceptionSummaryRead.model_validate(row) for row in summaries])


@exceptions_router.post(
    "/{exception_id}/ack",
    response_model=SuccessEnvelope[ExceptionLogRead],
    dependencies=[Depends(require_permission("exception:update"))],
)
async def acknowledge_exception(
    exception_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    row = await exception_log_repository.get_by_id(session, exception_id)
    if row is None:
        raise BusinessError(
            ErrorCode.NOT_FOUND,
            "Exception log not found",
            http_status=404,
        )
    # Re-acking is a no-op so the original handler and timestamp are preserved.
    if not row.acknowledged:
        await exception_log_repository.ack(
            session,
            row,
            user_id=context.user_id,
            at=_utcnow(),
        )
        await session.commit()
    return success(ExceptionLogRead.model_validate(row))


__all__: tuple[str, ...] = (
    "acknowledge_exception",
    "exceptions_router",
    "get_current_snapshot",
    "get_metric_history",
    "list_exceptions",
    "router",
    "summarize_exceptions",
)
