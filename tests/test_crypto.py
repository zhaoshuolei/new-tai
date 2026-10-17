import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import BusinessError, ErrorCode


def _tamper(token: str) -> str:
    """返回一个确定与原 token 不同的被篡改 token。"""
    tampered = token[:-4] + "AAAA"
    if tampered != token:
        return tampered
    # 末尾恰好已是 "AAAA" 时，改为翻转中间一个字符，保证结果必然不同。
    mid = len(token) // 2
    replacement = "B" if token[mid] == "A" else "A"
    return token[:mid] + replacement + token[mid + 1 :]


def test_round_trip_returns_plaintext() -> None:
    token = "mcpk2_example_value"
    assert decrypt_secret(encrypt_secret(token)) == token


def test_ciphertext_does_not_contain_plaintext() -> None:
    assert "mcpk2_example" not in encrypt_secret("mcpk2_example_value")


def test_tampered_ciphertext_raises_unreadable() -> None:
    original = encrypt_secret("x")
    tampered = _tamper(original)
    assert tampered != original

    with pytest.raises(BusinessError) as exc:
        decrypt_secret(tampered)
    assert exc.value.code == ErrorCode.TIANYANCHA_TOKEN_UNREADABLE
    assert exc.value.http_status == 503


def test_ciphertext_from_different_key_raises_unreadable() -> None:
    other_key = Fernet.generate_key().decode("utf-8")
    foreign = Fernet(other_key).encrypt(b"mcpk2_rotated_value")
    foreign_ciphertext = foreign.decode("utf-8")

    with pytest.raises(BusinessError) as exc:
        decrypt_secret(foreign_ciphertext)
    assert exc.value.code == ErrorCode.TIANYANCHA_TOKEN_UNREADABLE
    assert exc.value.http_status == 503


def test_valid_token_with_invalid_utf8_plaintext_raises_unreadable() -> None:
    # Fernet 校验通过但明文不是合法 UTF-8 时，同样必须映射为 8002，而不是
    # 把 UnicodeDecodeError 冒泡成 500。
    token = Fernet(settings.config_encryption_key).encrypt(b"\xff\xfe").decode("utf-8")

    with pytest.raises(BusinessError) as exc:
        decrypt_secret(token)
    assert exc.value.code == ErrorCode.TIANYANCHA_TOKEN_UNREADABLE
    assert exc.value.http_status == 503
