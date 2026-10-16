"""本地账号密码策略。"""

PASSWORD_MIN_LENGTH = 8
PASSWORD_POLICY_MESSAGE = (
    f"Password must be at least {PASSWORD_MIN_LENGTH} characters and include "
    "at least one letter and one digit"
)


def validate_password_policy(password: str) -> str:
    """校验密码长度与基础复杂度，返回原值供调用方继续使用。"""
    if (
        len(password) < PASSWORD_MIN_LENGTH
        or not any(character.isalpha() for character in password)
        or not any(character.isdigit() for character in password)
    ):
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    return password


__all__: tuple[str, ...] = (
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_POLICY_MESSAGE",
    "validate_password_policy",
)
