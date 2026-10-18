"""只读的活跃客户重名检查（设计 7.1）。

迁移 ``a7d3c9e15b42`` 在建立唯一索引前会阻断存量活跃重名；本脚本供运维在
升级前定位具体分组。脚本不写数据库、不写应用日志和审计日志：报告只出现在
操作者终端，或写入显式指定的 ``0600`` 私有文件。
"""

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_factory

_SAFE_TABLE_NAME = re.compile(r"[a-z0-9_]+")
REPAIR_COMMAND = (
    "python -m scripts.check_duplicate_customer_names --output duplicates.txt"
)


@dataclass(frozen=True)
class DuplicateNameGroup:
    """一个活跃重名分组：规范化名称与占用它的客户 ID。"""

    name: str
    customer_ids: tuple[int, ...]


async def _duplicate_active_name_rows(
    session: AsyncSession,
    *,
    table_name: str = "customer",
) -> dict[str, list[int]]:
    """按名称列的排序规则分组，返回 ``{规范化名称: [客户 ID, ...]}``。"""
    if _SAFE_TABLE_NAME.fullmatch(table_name) is None:
        raise ValueError("Unexpected table name for customer name check")

    quoted_table = f"`{table_name}`"
    rows = (
        await session.execute(
            sa.text(
                f"""
                SELECT duplicate_names.normalized_name, target.id
                FROM {quoted_table} AS target
                JOIN (
                    SELECT TRIM(name) AS normalized_name
                    FROM {quoted_table}
                    WHERE status = 'ACTIVE'
                    GROUP BY TRIM(name)
                    HAVING COUNT(*) > 1
                ) AS duplicate_names
                  ON duplicate_names.normalized_name = TRIM(target.name)
                WHERE target.status = 'ACTIVE'
                ORDER BY duplicate_names.normalized_name, target.id
                """
            )
        )
    ).all()

    groups: dict[str, list[int]] = {}
    for normalized_name, customer_id in rows:
        groups.setdefault(str(normalized_name), []).append(int(customer_id))
    return groups


async def find_duplicate_active_names(
    session: AsyncSession,
) -> list[DuplicateNameGroup]:
    """列出当前库中所有活跃重名分组，按名称排序。"""
    groups = await _duplicate_active_name_rows(session)
    return [
        DuplicateNameGroup(name=name, customer_ids=tuple(customer_ids))
        for name, customer_ids in groups.items()
    ]


def render_report(groups: list[DuplicateNameGroup]) -> str:
    """渲染终端/报告文本；仅在显式排查时输出名称。"""
    if not groups:
        return "No duplicate active customer names found.\n"
    lines = [
        f"{len(groups)} group(s) of duplicate active customer names found.",
        "Merge or delete the duplicates before running `alembic upgrade head`.",
        "",
    ]
    for index, group in enumerate(groups, start=1):
        customer_ids = ", ".join(str(customer_id) for customer_id in group.customer_ids)
        lines.append(f"{index}. name={group.name!r} customer_ids=[{customer_ids}]")
    lines.append("")
    lines.append(f"Re-run this read-only check with: {REPAIR_COMMAND}")
    return "\n".join(lines) + "\n"


def emit_report(report: str, output: Path | None) -> None:
    """写入 ``0600`` 私有文件；未指定路径时只打印到当前终端。"""
    if output is None:
        sys.stdout.write(report)
        sys.stdout.flush()
        return

    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # 已存在的文件不会因 open 的 mode 收紧权限，显式再设置一次。
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(report)
    except BaseException:
        os.close(descriptor)
        raise


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only check for duplicate active customer names. "
            "The report is printed to the terminal or written to --output."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the private report to this path with mode 0600",
    )
    return parser.parse_args(argv)


async def _run(output: Path | None) -> int:
    async with session_factory() as session:
        groups = await find_duplicate_active_names(session)
    emit_report(render_report(groups), output)
    return 1 if groups else 0


def main(argv: list[str] | None = None) -> None:
    arguments = _parse_args(argv)
    raise SystemExit(asyncio.run(_run(arguments.output)))


if __name__ == "__main__":
    main()


__all__: tuple[str, ...] = (
    "DuplicateNameGroup",
    "emit_report",
    "find_duplicate_active_names",
    "main",
    "render_report",
)
