"""基于 Redis 的固定窗口限流工具。

对一组键原子计数，任一键在窗口内超过阈值即拒绝请求，用于保护
天眼查等稀缺上游配额不被单个用户的误操作耗尽。
"""

from redis.asyncio import Redis

from app.core.errors import BusinessError, ErrorCode
from app.core.redis import key

SEARCH_WINDOW_SECONDS = 60
SEARCH_MAX_REQUESTS = 20
IMPORT_WINDOW_SECONDS = 3600
IMPORT_MAX_REQUESTS = 10

# 与登录限流同源的 Lua 脚本：逐个 INCR，首次自增时设置过期时间，
# 任一键超过阈值则返回 1。整个脚本在 Redis 内原子执行。
_RATE_LIMIT_SCRIPT = """
local exceeded = 0
for _, rate_key in ipairs(KEYS) do
    local count = redis.call('INCR', rate_key)
    if count == 1 then
        redis.call('EXPIRE', rate_key, ARGV[1])
    end
    if count > tonumber(ARGV[2]) then
        exceeded = 1
    end
end
return exceeded
"""


def tianyancha_rate_limit_key(*parts: str) -> str:
    """构造天眼查限流键，遵循 ``{app}:{env}:{domain}:{ident}`` 约定。"""
    return key("tianyancha", *parts)


async def enforce_rate_limit(
    redis: Redis,
    keys: tuple[str, ...],
    *,
    window_seconds: int,
    max_requests: int,
    message: str,
) -> None:
    """预占一个或多个限流桶，任一桶超出阈值时抛出 429。"""
    exceeded = await redis.eval(
        _RATE_LIMIT_SCRIPT,
        len(keys),
        *keys,
        str(window_seconds),
        str(max_requests),
    )
    if exceeded != 0:
        raise BusinessError(
            ErrorCode.RATE_LIMITED,
            message,
            http_status=429,
        )


__all__: tuple[str, ...] = (
    "IMPORT_MAX_REQUESTS",
    "IMPORT_WINDOW_SECONDS",
    "SEARCH_MAX_REQUESTS",
    "SEARCH_WINDOW_SECONDS",
    "enforce_rate_limit",
    "tianyancha_rate_limit_key",
)
