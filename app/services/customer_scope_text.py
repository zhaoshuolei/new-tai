"""完整经营范围解析（设计 6.4 / D1）。

工商登记接口返回的「经营范围」被上游截断，以 ``...`` 结尾。变更历史
（``get_historical_registration``）中「经营范围」变更记录的「变更后原文」
包含完整文本，可据此还原。
"""

_TRUNCATION_MARKERS = ("...", "…")


def resolve_full_business_scope(
    displayed: str | None, change_records: list[tuple[str, str]]
) -> tuple[str | None, bool]:
    """由截断经营范围与变更记录还原完整经营范围。

    返回 ``(经营范围, 是否截断)``。``change_records`` 为 ``(变更时间, 变更后原文)``
    列表，变更时间形如 ``YYYY-MM-DD``，缺失时为空串。

    规则（设计 6.4）：

    - ``displayed`` 为 ``None`` 时返回 ``(None, False)``。
    - 不以截断标记结尾的文本视为已完整，直接返回且不查变更历史。
    - 去掉结尾标记得到前缀；前缀为空时保留原文本并标记截断。
    - 取「变更后原文」以前缀开头且变更时间最早的记录视为最新，即按日期降序取第一条。
    - 无匹配记录时返回 ``(displayed, True)``。
    """
    if displayed is None:
        return None, False

    stripped = displayed.rstrip()
    if not stripped.endswith(_TRUNCATION_MARKERS):
        return displayed, False

    if stripped.endswith("..."):
        prefix = stripped[:-3].strip()
    else:
        prefix = stripped[:-1].strip()
    if not prefix:
        return displayed, True

    latest_date: str | None = None
    latest_text: str | None = None
    for date, text in change_records:
        if not text.startswith(prefix):
            continue
        # 空日期视为最旧（空串排序在 ``YYYY-MM-DD`` 之前）。
        if latest_date is None or date > latest_date:
            latest_date = date
            latest_text = text

    if latest_text is None:
        return displayed, True
    return latest_text, False
