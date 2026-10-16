"""完整经营范围解析纯函数测试（设计 6.4 / D1）。"""

from pathlib import Path

from app.clients.tianyancha import (
    parse_change_scope_records,
    parse_registration_table,
)
from app.services.customer_scope_text import resolve_full_business_scope

FIXTURES = Path(__file__).parent / "fixtures" / "tianyancha"


# ---------------------------------------------------------------------------
# 简报中的四个用例
# ---------------------------------------------------------------------------


def test_returns_latest_full_text_matching_prefix() -> None:
    displayed = (
        "一般项目：软件开发；技术服务、技术开发、技术咨询、技术交流、技术转让、"
        "技术推广；计算机系统服务；计算机软硬件及辅助设备批发；计算机软硬件及辅助"
        "设备零售；服装服饰批发；服装服饰零售；鞋帽批发；鞋帽零售；皮革制品销售；"
        "针纺织品销售；日用品销售；玩具销售；文具用品批发；文具用品零售；体育用品"
        "及器材批发；体育用品及器材零售；家..."
    )
    records = [
        ("2022-03-11", displayed[:-3] + "用品销售。（完整）"),
        ("2018-11-05", "其他经营范围"),
    ]

    full, truncated = resolve_full_business_scope(displayed, records)

    assert full is not None
    assert full.startswith(displayed[:-3])
    assert full.endswith("（完整）")
    assert truncated is False


def test_no_matching_record_keeps_truncated_and_flags() -> None:
    full, truncated = resolve_full_business_scope(
        "一般项目：A...", [("2020-01-01", "完全无关")]
    )

    assert full == "一般项目：A..."
    assert truncated is True


def test_none_displayed_returns_none_not_truncated() -> None:
    assert resolve_full_business_scope(None, []) == (None, False)


def test_older_record_does_not_beat_newer_when_ordering_is_shuffled() -> None:
    displayed = "一般项目：A..."
    records = [
        ("2010-01-01", "一般项目：A旧版本"),
        ("2022-01-01", "一般项目：A新版本"),
    ]

    full, _ = resolve_full_business_scope(displayed, records)

    assert full == "一般项目：A新版本"


# ---------------------------------------------------------------------------
# 控制器裁决 A / B
# ---------------------------------------------------------------------------


def test_complete_text_without_marker_skips_history() -> None:
    # RULING A：不以截断标记结尾的文本视为已完整，绝不查变更历史。
    displayed = "一般项目：软件开发；技术服务。"
    records = [("2022-03-11", displayed + "（若被匹配会替换）")]

    full, truncated = resolve_full_business_scope(displayed, records)

    assert full == displayed
    assert truncated is False


def test_marker_only_displayed_stays_truncated() -> None:
    # RULING B：去掉标记后前缀为空，不得让空前缀匹配任何记录。
    full, truncated = resolve_full_business_scope(
        "...", [("2022-03-11", "任意完整文本")]
    )

    assert full == "..."
    assert truncated is True


def test_lowercase_ellipsis_marker_is_stripped() -> None:
    displayed = "一般项目：软件开发…  "
    records = [("2022-01-01", "一般项目：软件开发完整版本")]

    full, truncated = resolve_full_business_scope(displayed, records)

    assert full == "一般项目：软件开发完整版本"
    assert truncated is False


# ---------------------------------------------------------------------------
# fixture 驱动
# ---------------------------------------------------------------------------


def test_fixture_truncated_scope_resolves_to_2022_full_text() -> None:
    displayed = parse_registration_table(
        (FIXTURES / "registration.md").read_text(encoding="utf-8")
    ).business_scope
    assert displayed is not None
    records = parse_change_scope_records(
        (FIXTURES / "historical_registration_excerpt.md").read_text(encoding="utf-8")
    )

    full, truncated = resolve_full_business_scope(displayed, records)

    assert truncated is False
    assert full is not None
    assert records[0][0] == "2022-03-11"
    assert full == records[0][1]
    assert len(full) > len(displayed)
    assert full.endswith("（不得从事国家和本市产业政策禁止和限制类项目的经营活动。）")


def test_fixture_2018_record_alone_does_not_match() -> None:
    displayed = parse_registration_table(
        (FIXTURES / "registration.md").read_text(encoding="utf-8")
    ).business_scope
    assert displayed is not None
    records = parse_change_scope_records(
        (FIXTURES / "historical_registration_excerpt.md").read_text(encoding="utf-8")
    )
    only_2018 = [record for record in records if record[0] == "2018-11-05"]
    assert only_2018

    full, truncated = resolve_full_business_scope(displayed, only_2018)

    assert full == displayed
    assert truncated is True
