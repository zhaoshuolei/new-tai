from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode

# 模块级构造一次；密钥来自配置 config_encryption_key。
_FERNET = Fernet(settings.config_encryption_key)


def encrypt_secret(plain: str) -> str:
    """加密敏感明文（如第三方 token），返回 Fernet token 字符串。"""
    return _FERNET.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """解密 Fernet token。

    解密失败（密文被篡改、或使用了不同密钥加密）时抛出 BusinessError，
    错误消息与异常链均不包含密文或明文。
    """
    try:
        return _FERNET.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        raise BusinessError(
            ErrorCode.TIANYANCHA_TOKEN_UNREADABLE,
            "Stored credential cannot be read; please re-save it",
            http_status=503,
        ) from None


__all__: tuple[str, ...] = ("decrypt_secret", "encrypt_secret")
