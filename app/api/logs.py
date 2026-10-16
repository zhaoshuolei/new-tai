"""操作与登录日志查询接口。"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require_permission
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.audit_log import AuditResult
from app.models.login_log import LoginResultEnum, LoginType
from app.repositories import audit_log as audit_log_repository
from app.repositories import login_log as login_log_repository
from app.schemas.logs import AuditLogRead, LoginLogRead

router = APIRouter(prefix="/logs", tags=["日志"])


@router.get(
    "/operations",
    response_model=SuccessEnvelope[Page[AuditLogRead]],
    dependencies=[Depends(require_permission("audit:read"))],
)
async def list_operation_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    user_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: int | None = None,
    result: AuditResult | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    logs, total = await audit_log_repository.list_audit_logs(
        session,
        page=page,
        page_size=page_size,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        start_time=start_time,
        end_time=end_time,
        keyword=keyword,
    )
    return success(
        Page[AuditLogRead](
            items=[AuditLogRead.model_validate(log) for log in logs],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/logins",
    response_model=SuccessEnvelope[Page[LoginLogRead]],
    dependencies=[Depends(require_permission("login_log:read"))],
)
async def list_login_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    user_id: int | None = None,
    username: str | None = None,
    login_type: LoginType | None = None,
    result: LoginResultEnum | None = None,
    ip: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    logs, total = await login_log_repository.list_login_logs(
        session,
        page=page,
        page_size=page_size,
        user_id=user_id,
        username=username,
        login_type=login_type,
        result=result,
        ip=ip,
        start_time=start_time,
        end_time=end_time,
    )
    return success(
        Page[LoginLogRead](
            items=[LoginLogRead.model_validate(log) for log in logs],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


__all__: tuple[str, ...] = (
    "list_login_logs",
    "list_operation_logs",
    "router",
)
