"""天眼查集成配置服务。

负责 token 的加密保存、配置状态读取与客户端构造。token 的明文与密文都不
出现在返回结构、异常消息或日志中（设计 7.2 / 13）。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tianyancha import TianyanchaClient
from app.core.auth_context import AuthContext
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import BusinessError, ErrorCode
from app.repositories.integration_config import get_by_provider, upsert
from app.services.audit import audited

logger = logging.getLogger(__name__)

_PROVIDER = "tianyancha"


async def get_tianyancha_status(session: AsyncSession) -> dict[str, object]:
    """返回天眼查配置状态，不含任何 token 明文或密文。"""
    config = await get_by_provider(session, _PROVIDER)
    if config is None:
        return {"configured": False, "updated_at": None, "updated_by": None}
    return {
        "configured": True,
        "updated_at": config.updated_at,
        "updated_by": config.updated_by,
    }


@audited(
    action="integration:update",
    resource_type="integration",
)
async def save_tianyancha_token(
    session: AsyncSession,
    api_key: str,
    *,
    context: AuthContext,
) -> None:
    """加密保存天眼查 token。

    空值（含纯空白）按参数错误处理。写用例由服务层负责事务边界，自行 commit。
    """
    token = api_key.strip()
    if not token:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Tianyancha api key must not be empty",
            http_status=422,
        )

    ciphertext = encrypt_secret(token)
    await upsert(session, _PROVIDER, ciphertext, updated_by=context.user_id)
    # 写用例由服务层负责事务边界，与其他写服务一致。
    await session.commit()


async def get_tianyancha_client(session: AsyncSession) -> TianyanchaClient:
    """解密已配置的 token 并返回天眼查客户端。

    未配置抛 8001（409）；密文无法解密时记录不含明文/密文的错误日志并让
    ``decrypt_secret`` 的 8002 异常继续上抛。
    """
    config = await get_by_provider(session, _PROVIDER)
    if config is None:
        raise BusinessError(
            ErrorCode.TIANYANCHA_NOT_CONFIGURED,
            "Tianyancha is not configured",
            http_status=409,
        )

    try:
        api_key = decrypt_secret(config.secret_ciphertext)
    except BusinessError:
        logger.error(
            "Failed to decrypt stored Tianyancha credential; re-save required"
        )
        raise

    return TianyanchaClient(api_key=api_key)


async def test_tianyancha_connection(
    session: AsyncSession,
    *,
    context: AuthContext,
) -> None:
    """校验 token 可用性；成功返回 None，失败按客户端 BusinessError 上抛。

    该调用会消耗 1 次上游额度（设计 12.2）。
    """
    client = await get_tianyancha_client(session)
    await client.test_connection()


__all__: tuple[str, ...] = (
    "get_tianyancha_client",
    "get_tianyancha_status",
    "save_tianyancha_token",
    "test_tianyancha_connection",
)
