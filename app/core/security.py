"""密码哈希与校验。"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_PASSWORD_HASHER = PasswordHasher()


def hash_password(plain: str) -> str:
    """使用 Argon2 生成密码哈希。"""
    return _PASSWORD_HASHER.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与 Argon2 哈希是否匹配。"""
    try:
        return _PASSWORD_HASHER.verify(hashed, plain)
    except InvalidHashError, VerificationError, VerifyMismatchError:
        return False


__all__: tuple[str, ...] = ("hash_password", "verify_password")
