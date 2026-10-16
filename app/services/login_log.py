"""登录日志写入服务。"""

from app.core.best_effort import run_best_effort
from app.core.request_context import RequestMeta, bounded_request_id
from app.db.session import session_factory
from app.models.login_log import LoginLog, LoginResultEnum, LoginType
from app.repositories import login_log as login_log_repository


async def record_login_log(
    *,
    user_id: int | None,
    username: str | None,
    login_type: LoginType,
    result: LoginResultEnum,
    reason: str | None,
    request_meta: RequestMeta | None,
    device_id: str | None = None,
) -> None:
    """Persist one login attempt in an independent best-effort transaction."""

    async def write_entry() -> None:
        row = LoginLog(
            user_id=user_id,
            username=username,
            login_type=login_type,
            result=result,
            reason=reason,
            ip=request_meta.ip if request_meta is not None else None,
            user_agent=(request_meta.user_agent if request_meta is not None else None),
            device_id=device_id,
            request_id=bounded_request_id(
                request_meta.request_id if request_meta is not None else ""
            ),
        )
        async with session_factory() as session:
            await login_log_repository.add(session, row)
            await session.commit()

    await run_best_effort(
        write_entry,
        description=f"record {login_type.value} login log",
    )


__all__: tuple[str, ...] = ("record_login_log",)
