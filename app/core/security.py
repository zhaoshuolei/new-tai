"""密码哈希、校验与访问令牌签名。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode

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


def create_access_token(user_id: int, token_version: int) -> str:
    """签发带用户版本号的短期访问令牌。"""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "ver": token_version,
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """校验访问令牌并返回其声明。"""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "ver", "jti", "exp"]},
        )
    except ExpiredSignatureError as error:
        raise BusinessError(
            ErrorCode.TOKEN_EXPIRED,
            "Token expired",
            http_status=401,
        ) from error
    except InvalidTokenError as error:
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        ) from error

    if not isinstance(claims["sub"], str) or not claims["sub"].isdigit():
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )
    if (
        not isinstance(claims["ver"], int)
        or isinstance(claims["ver"], bool)
        or not isinstance(claims["jti"], str)
        or not claims["jti"]
    ):
        raise BusinessError(
            ErrorCode.TOKEN_INVALID,
            "Token invalid",
            http_status=401,
        )
    return claims


__all__: tuple[str, ...] = (
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "verify_password",
)
