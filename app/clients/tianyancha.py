"""天眼查 MCP 客户端与 Markdown 解析工具。

通过 Streamable HTTP JSON-RPC 直接调用天眼查 MCP，不依赖外部 CLI。
解析函数为纯函数，便于离线测试；网络交互全部走 ``httpx``，可由调用方注入
``transport``（测试使用 ``httpx.MockTransport``）。
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import BusinessError, ErrorCode

MCP_URL = "https://mcp.tianyancha.com/mcp"
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
EMPTY_RESULT_MARKER = "空结果：未发现该维度记录"

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TOTAL_RE = re.compile(r"总数\s*(\d+)\s*条")
_H5_PREFIX = "##### "
_H6_PREFIX = "###### "

_RATE_LIMIT_CODE = 300004
_QUOTA_CODES = (300006, 300007)
_AUTH_CODES = (300002, 300003, 300009, 302004)

_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.TIANYANCHA_UNAVAILABLE: "Tianyancha upstream service unavailable",
    ErrorCode.TIANYANCHA_RATE_LIMITED: "Tianyancha rate limit exceeded",
    ErrorCode.TIANYANCHA_QUOTA_EXCEEDED: "Tianyancha quota exceeded",
    ErrorCode.TIANYANCHA_AUTH_FAILED: "Tianyancha authentication failed",
    ErrorCode.TIANYANCHA_NOT_FOUND: "Tianyancha company not found",
    ErrorCode.TIANYANCHA_MALFORMED_RESPONSE: (
        "Tianyancha returned an unparseable response"
    ),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    credit_code: str | None
    name: str
    legal_person: str | None
    reg_status: str | None
    company_id: str


@dataclass(frozen=True, slots=True)
class RegistrationInfo:
    name: str
    credit_code: str
    legal_person: str | None
    registered_capital: str | None
    established_date: str | None
    business_scope: str | None
    reg_status: str | None


# ---------------------------------------------------------------------------
# Markdown parsing (pure functions)
# ---------------------------------------------------------------------------


def _split_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    inner = stripped[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        cell and set(cell) <= {"-", ":"} and "-" in cell for cell in cells
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text == "-":
        return None
    return text


def _first_index(cells: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, cell in enumerate(cells):
        key = cell.strip()
        if key and key not in index:
            index[key] = position
    return index


def _cell(cells: list[str], header: dict[str, int], name: str) -> str | None:
    position = header.get(name)
    if position is None or position >= len(cells):
        return None
    return _clean(cells[position])


def parse_search_table(markdown: str) -> tuple[list[Candidate], int]:
    """解析候选企业列表。空结果返回 ``([], 0)``。"""
    if EMPTY_RESULT_MARKER in markdown:
        return [], 0

    total_match = _TOTAL_RE.search(markdown)
    total = int(total_match.group(1)) if total_match is not None else 0

    candidates: list[Candidate] = []
    header: dict[str, int] | None = None
    for line in markdown.splitlines():
        cells = _split_row(line)
        if cells is None or _is_separator(cells):
            continue
        if header is None:
            if "企业名称" in cells and "企业ID" in cells:
                header = _first_index(cells)
            continue
        # 数据行的首列是序号；表头、分隔行、其它表格会被跳过。
        if not cells[0].isdigit():
            continue
        name = _cell(cells, header, "企业名称")
        if not name:
            continue
        candidates.append(
            Candidate(
                credit_code=_cell(cells, header, "统一社会信用代码"),
                name=name,
                legal_person=_cell(cells, header, "法定代表人"),
                reg_status=_cell(cells, header, "登记状态"),
                company_id=_cell(cells, header, "企业ID") or "",
            )
        )
    return candidates, total


_REGISTRATION_FIELDS: dict[str, str] = {
    "企业名称": "name",
    "统一社会信用代码": "credit_code",
    "法定代表人": "legal_person",
    "注册资本": "registered_capital",
    "成立日期": "established_date",
    "经营范围": "business_scope",
    "登记状态": "reg_status",
}


def parse_registration_table(markdown: str) -> RegistrationInfo:
    """按中文字段名映射概览字段表；缺字段留空，不猜测。"""
    values: dict[str, str] = {}
    for line in markdown.splitlines():
        cells = _split_row(line)
        if cells is None or _is_separator(cells) or len(cells) < 2:
            continue
        attribute = _REGISTRATION_FIELDS.get(cells[0])
        if attribute is None or attribute in values:
            continue
        value = _clean(cells[1])
        if value is not None:
            values[attribute] = value

    return RegistrationInfo(
        name=values.get("name", ""),
        credit_code=values.get("credit_code", ""),
        legal_person=values.get("legal_person"),
        registered_capital=values.get("registered_capital"),
        established_date=values.get("established_date"),
        business_scope=values.get("business_scope"),
        reg_status=values.get("reg_status"),
    )


def _extract_post_change_text(block: list[str]) -> str:
    collecting = False
    buffer: list[str] = []
    for line in block:
        if line.startswith(_H6_PREFIX):
            collecting = "变更后原文" in line
            continue
        if collecting:
            buffer.append(line)
    return "\n".join(buffer).strip()


def parse_change_scope_records(markdown: str) -> list[tuple[str, str]]:
    """返回 ``(变更时间, 变更后原文)``，仅限「经营范围」变更记录。"""
    lines = markdown.splitlines()
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(_H5_PREFIX) and "经营范围" in line:
            date_match = _DATE_RE.search(line)
            end = index + 1
            while end < len(lines) and not lines[end].startswith(_H5_PREFIX):
                end += 1
            text = _extract_post_change_text(lines[index + 1 : end])
            if date_match is not None and text:
                records.append((date_match.group(0), text))
            index = end
        else:
            index += 1
    return records


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _business_error(code: ErrorCode, http_status: int) -> BusinessError:
    return BusinessError(code, _MESSAGES[code], http_status=http_status)


def _payload_text(payload_or_text: object) -> str:
    if isinstance(payload_or_text, str):
        return payload_or_text
    if isinstance(payload_or_text, bytes):
        return payload_or_text.decode("utf-8", "replace")
    try:
        return json.dumps(payload_or_text, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload_or_text)


def _has_code(text: str, *codes: int) -> bool:
    return any(str(code) in text for code in codes)


def map_upstream_error(
    status_code: int | None, payload_or_text: object
) -> BusinessError:
    """把上游状态码与响应体映射为通用 ``BusinessError``。

    ``message`` 固定，不回传上游原文，避免泄漏 token 或原始响应体。
    """
    text = _payload_text(payload_or_text)
    lowered = text.lower()

    if (
        status_code == 429
        or _has_code(text, _RATE_LIMIT_CODE)
        or "rate_limited" in lowered
    ):
        return _business_error(ErrorCode.TIANYANCHA_RATE_LIMITED, 429)

    if (
        status_code == 402
        or _has_code(text, *_QUOTA_CODES)
        or "quota_exceeded" in lowered
        or "insufficient_balance" in lowered
    ):
        return _business_error(ErrorCode.TIANYANCHA_QUOTA_EXCEEDED, 402)

    if (
        status_code == 401
        or _has_code(text, *_AUTH_CODES)
        or "invalid_token" in lowered
    ):
        return _business_error(ErrorCode.TIANYANCHA_AUTH_FAILED, 502)

    if status_code is None or (isinstance(status_code, int) and status_code >= 500):
        return _business_error(ErrorCode.TIANYANCHA_UNAVAILABLE, 502)

    return _business_error(ErrorCode.TIANYANCHA_MALFORMED_RESPONSE, 502)


def _unavailable_error() -> BusinessError:
    return _business_error(ErrorCode.TIANYANCHA_UNAVAILABLE, 502)


def _not_found_error() -> BusinessError:
    return _business_error(ErrorCode.TIANYANCHA_NOT_FOUND, 404)


def _malformed_error() -> BusinessError:
    return _business_error(ErrorCode.TIANYANCHA_MALFORMED_RESPONSE, 502)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _last_sse_data(text: str) -> str | None:
    last: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("data:"):
            data = stripped[len("data:") :].strip()
            if data:
                last = data
    return last


def _decode_json_payload(response: httpx.Response) -> dict[str, Any]:
    raw = response.text
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        data = _last_sse_data(raw)
        if data is None:
            raise _malformed_error()
        raw = data
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise map_upstream_error(response.status_code, raw) from None
    if not isinstance(payload, dict):
        raise _malformed_error()
    return payload


def _extract_tool_text(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if error:
        raise map_upstream_error(200, payload)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise _malformed_error()
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise _malformed_error()
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        raise _malformed_error()
    return text


class TianyanchaClient:
    """调用天眼查 MCP 的 Streamable HTTP 客户端。"""

    def __init__(
        self,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._timeout = DEFAULT_TIMEOUT
        self._session_id: str | None = None
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self._api_key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id is not None:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _post(self, body: dict[str, Any], *, operation: str) -> httpx.Response:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    MCP_URL, json=body, headers=self._headers()
                )
        except httpx.HTTPError as error:
            logger.warning(
                "Tianyancha request failed: operation=%s error=%s",
                operation,
                type(error).__name__,
            )
            raise _unavailable_error() from error
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "Tianyancha request: operation=%s status=%s duration_ms=%s",
            operation,
            response.status_code,
            duration_ms,
        )
        return response

    async def _ensure_session(self) -> None:
        if self._session_id is not None:
            return
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tianxiadmin", "version": "1.0.0"},
            },
        }
        response = await self._post(body, operation="initialize")
        if response.status_code >= 400:
            raise map_upstream_error(response.status_code, response.text)
        session_id = response.headers.get("mcp-session-id")
        if not session_id:
            raise _malformed_error()
        self._session_id = session_id

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        await self._ensure_session()
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        response = await self._post(body, operation=f"tools/call:{tool_name}")
        if response.status_code >= 400:
            raise map_upstream_error(response.status_code, response.text)
        payload = _decode_json_payload(response)
        return _extract_tool_text(payload)

    async def test_connection(self) -> None:
        await self._call_tool(
            "search_companies", {"query": "test", "page": 1, "page_size": 1}
        )

    async def search_candidates(
        self, keyword: str, *, page: int, page_size: int
    ) -> tuple[list[Candidate], int]:
        text = await self._call_tool(
            "search_companies",
            {"query": keyword, "page": page, "page_size": page_size},
        )
        return parse_search_table(text)

    async def fetch_registration(self, credit_code: str) -> RegistrationInfo:
        text = await self._call_tool(
            "call_tool",
            {
                "company_name": credit_code,
                "tool_name": "get_company_registration_info",
                "arguments": {},
            },
        )
        if EMPTY_RESULT_MARKER in text:
            raise _not_found_error()
        return parse_registration_table(text)

    async def fetch_historical_registration_text(self, credit_code: str) -> str:
        text = await self._call_tool(
            "call_tool",
            {
                "company_name": credit_code,
                "tool_name": "get_historical_registration",
                "arguments": {},
            },
        )
        if EMPTY_RESULT_MARKER in text:
            # 无历史工商记录不是错误，返回空文本，由调用方回退为截断标记。
            return ""
        return text


__all__: tuple[str, ...] = (
    "DEFAULT_TIMEOUT",
    "EMPTY_RESULT_MARKER",
    "MCP_URL",
    "Candidate",
    "RegistrationInfo",
    "TianyanchaClient",
    "map_upstream_error",
    "parse_change_scope_records",
    "parse_registration_table",
    "parse_search_table",
)
