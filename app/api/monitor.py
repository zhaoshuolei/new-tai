"""系统监控接口。"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require_permission
from app.core.redis import get_redis
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.monitor import CurrentSnapshotRead, SystemMetricRead
from app.services import monitor as monitor_service

router = APIRouter(
    prefix="/monitor/system",
    tags=["系统监控"],
    dependencies=[Depends(require_permission("monitor:read"))],
)


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


__all__: tuple[str, ...] = (
    "get_current_snapshot",
    "get_metric_history",
    "router",
)
