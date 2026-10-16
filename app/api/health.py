from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.db.session import get_session
from app.services.health import check_database, check_redis

router = APIRouter(tags=["健康"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    if not await check_database(session) or not await check_redis(redis_client):
        raise HTTPException(status_code=503, detail="Service unavailable")

    return {"status": "ok"}


__all__: tuple[str, ...] = ("liveness", "readiness", "router")
