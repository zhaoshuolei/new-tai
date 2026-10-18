"""客户名称规范化与查找键。

写入路径统一去首尾空白；Excel 解析用查找键模拟 MySQL
``utf8mb4_0900_ai_ci`` 的大小写、重音不敏感比较意图（见设计 7.1）。
"""

import unicodedata

from app.core.errors import BusinessError, ErrorCode


def normalize_customer_name(value: str) -> str:
    """去掉首尾空白；结果为空时按字段错误 7003 拒绝。"""
    name = value.strip()
    if not name:
        raise BusinessError(
            ErrorCode.CUSTOMER_INVALID_FIELD,
            "Customer name is required",
            http_status=422,
        )
    return name


def customer_name_lookup_key(value: str) -> str:
    """进程内名称匹配键：NFKC、去组合符、去首尾空白、casefold。

    数据库侧的最终判定仍由 ``utf8mb4_0900_ai_ci`` 唯一索引负责；此键只用于
    Excel/API 导入前的进程内查找，避免逐字节比较带来的假阴性。
    """
    normalized = unicodedata.normalize("NFKC", value)
    decomposed = unicodedata.normalize("NFD", normalized)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return without_marks.strip().casefold()


__all__: tuple[str, ...] = (
    "customer_name_lookup_key",
    "normalize_customer_name",
)
