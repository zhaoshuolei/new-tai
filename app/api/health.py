from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.db.session import get_session

router = APIRouter(tags=["健康"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    try:
        await session.execute(select(1))
        if not await redis_client.ping():
            raise RuntimeError("Redis ping failed")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc

    return {"status": "ok"}


__all__: tuple[str, ...] = ("liveness", "readiness", "router")
