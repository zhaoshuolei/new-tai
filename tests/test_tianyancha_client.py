"""天眼查 MCP 客户端与为纯解析函数的离线测试。

全部网络交互使用 ``httpx.MockTransport``，不发起真实请求。
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.clients.tianyancha import (
    Candidate,
    RegistrationInfo,
    TianyanchaClient,
    map_upstream_error,
    parse_change_scope_records,
    parse_registration_table,
    parse_search_table,
)
from app.core.errors import BusinessError, ErrorCode

FIXTURES = Path(__file__).parent / "fixtures" / "tianyancha"


def _read_extra(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def test_parse_search_table_extracts_candidates_and_total() -> None:
    candidates, total = parse_search_table(_read_extra("search_companies.md"))

    assert total == 111
    assert len(candidates) == 4
    first = candidates[0]
    assert first == Candidate(
        credit_code="911100007693890511",
        name="乐视网信息技术（北京）股份有限公司",
        legal_person="刘延峰",
        reg_status="存续",
        company_id="14427175",
    )


def test_parse_search_table_keeps_dash_credit_code_as_none() -> None:
    candidates, _ = parse_search_table(_read_extra("search_companies.md"))

    hong_kong = candidates[3]
    assert hong_kong.credit_code is None
    assert hong_kong.name == "樂視網信息技術（香港）有限公司"
    assert hong_kong.company_id == "3086162174"


def test_parse_search_table_duplicate_established_date_column_parses_row() -> None:
    # 真实表头中「成立日期」出现两次；索引映射必须保留首次出现且不崩溃。
    markdown = (
        "| # | 企业名称 | 统一社会信用代码 | 登记状态 | 法定代表人 | 注册资本 "
        "| 成立日期 | 企业类型 | 成立日期 | 企业ID | 匹配类型 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | 乐视网信息技术（北京）股份有限公司 | 911100007693890511 | 存续 "
        "| 刘延峰 | 398944.0192万人民币 | 2004-11-10 00:00:00.0 | 1 "
        "| 2004-11-10 00:00:00.0 | 14427175 | 公司名称匹配 |\n"
    )

    candidates, _ = parse_search_table(markdown)

    assert len(candidates) == 1
    assert candidates[0].name == "乐视网信息技术（北京）股份有限公司"
    assert candidates[0].credit_code == "911100007693890511"
    assert candidates[0].legal_person == "刘延峰"
    assert candidates[0].reg_status == "存续"
    assert candidates[0].company_id == "14427175"


def test_parse_search_table_ignores_non_data_rows() -> None:
    markdown = (
        "# 企业候选列表\n\n"
        "> 展示条数：总数 7 条；本次原始返回 1 条。\n\n"
        "| # | 企业名称 | 统一社会信用代码 | 登记状态 | 法定代表人 | 注册资本 "
        "| 成立日期 | 企业类型 | 成立日期 | 企业ID | 匹配类型 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | 示例公司 | 91110000TEST000001 | 存续 | 张三 | 100万人民币 "
        "| 2020-01-01 00:00:00.0 | 1 | 2020-01-01 00:00:00.0 | 42 | 公司名称匹配 |\n\n"
        "## 上游标识明细\n\n"
        "| 来源路径（原键） | 所属记录 | 原值（JSON） |\n"
        "|---|---|---|\n"
        "| items[0].id | 示例公司 | 42 |\n"
    )

    candidates, total = parse_search_table(markdown)

    assert total == 7
    assert len(candidates) == 1
    assert candidates[0].company_id == "42"


def test_empty_result_marker_overrides_any_table_rows() -> None:
    # 空结果标记必须压过任何可解析的表格行：去掉空结果守卫后，此输入会
    # 解析出非空候选列表，因此该断言能真正约束该行为。
    markdown = (
        "# 企业候选列表\n\n"
        "> 展示条数：总数 111 条；本次原始返回 0 条。\n\n"
        "> 空结果：未发现该维度记录\n\n"
        "| # | 企业名称 | 统一社会信用代码 | 登记状态 | 法定代表人 | 注册资本 "
        "| 成立日期 | 企业类型 | 成立日期 | 企业ID | 匹配类型 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | 乐视网信息技术（北京）股份有限公司 | 911100007693890511 | 存续 "
        "| 刘延峰 | 398944.0192万人民币 | 2004-11-10 00:00:00.0 | 1 "
        "| 2004-11-10 00:00:00.0 | 14427175 | 公司名称匹配 |\n"
    )

    candidates, total = parse_search_table(markdown)

    assert candidates == []
    assert total == 0


def test_empty_result_text_is_zero_candidates() -> None:
    candidates, total = parse_search_table("空结果：未发现该维度记录")

    assert candidates == []
    assert total == 0


def test_parse_registration_table_maps_fields() -> None:
    info = parse_registration_table(_read_extra("registration.md"))

    assert info == RegistrationInfo(
        name="乐视网信息技术（北京）股份有限公司",
        credit_code="911100007693890511",
        legal_person="刘延峰",
        registered_capital="398944.0192万人民币",
        established_date="2004-11-10",
        business_scope=info.business_scope,
        reg_status="存续",
    )
    assert info.credit_code == "911100007693890511"
    assert info.reg_status == "存续"
    assert info.legal_person == "刘延峰"
    assert info.established_date == "2004-11-10"
    assert info.business_scope is not None
    assert info.business_scope.endswith("...")


def test_parse_change_scope_records_returns_two_scope_changes() -> None:
    markdown = _read_extra("historical_registration_excerpt.md")
    records = parse_change_scope_records(markdown)

    assert [date for date, _ in records] == ["2022-03-11", "2018-11-05"]

    scope = parse_registration_table(_read_extra("registration.md")).business_scope
    assert scope is not None
    prefix = scope[:-3]  # 去掉结尾的 "..."
    assert prefix
    assert records[0][1].startswith(prefix)
    assert not records[1][1].startswith(prefix)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_map_upstream_error_invalid_token() -> None:
    error = map_upstream_error(
        401, {"error": "invalid_token", "error_description": "invalid MCP API key"}
    )
    assert error.code == ErrorCode.TIANYANCHA_AUTH_FAILED
    assert error.http_status == 502


def test_map_upstream_error_quota_by_status() -> None:
    error = map_upstream_error(402, {"code": 300007})
    assert error.code == ErrorCode.TIANYANCHA_QUOTA_EXCEEDED
    assert error.http_status == 402


def test_map_upstream_error_quota_by_documented_text() -> None:
    error = map_upstream_error(200, "quota_exceeded")
    assert error.code == ErrorCode.TIANYANCHA_QUOTA_EXCEEDED
    assert error.http_status == 402


def test_map_upstream_error_rate_limit() -> None:
    error = map_upstream_error(429, {"code": 300004})
    assert error.code == ErrorCode.TIANYANCHA_RATE_LIMITED
    assert error.http_status == 429


def test_map_upstream_error_rate_limit_by_text() -> None:
    error = map_upstream_error(200, {"error": "rate_limited"})
    assert error.code == ErrorCode.TIANYANCHA_RATE_LIMITED
    assert error.http_status == 429


def test_map_upstream_error_server_error() -> None:
    error = map_upstream_error(500, "internal server error")
    assert error.code == ErrorCode.TIANYANCHA_UNAVAILABLE
    assert error.http_status == 502


def test_map_upstream_error_network_maps_to_unavailable() -> None:
    error = map_upstream_error(None, "connection reset")
    assert error.code == ErrorCode.TIANYANCHA_UNAVAILABLE
    assert error.http_status == 502


def test_map_upstream_error_unparseable_payload_maps_to_malformed() -> None:
    error = map_upstream_error(200, "not a recognized payload")
    assert error.code == ErrorCode.TIANYANCHA_MALFORMED_RESPONSE
    assert error.http_status == 502


def test_map_upstream_error_does_not_leak_raw_body() -> None:
    secret = "super-secret-token"
    error = map_upstream_error(500, f"failure {secret}")
    assert secret not in error.message


# ---------------------------------------------------------------------------
# Transport (httpx.MockTransport only)
# ---------------------------------------------------------------------------

INITIALIZE_RESPONSE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "serverInfo": {"name": "tianyancha", "version": "1.0"},
    },
}


def _tools_result(request_id: int, text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


async def test_client_search_initializes_and_reuses_session() -> None:
    search_text = _read_extra("search_companies.md")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-abc"},
                json=INITIALIZE_RESPONSE,
            )
        assert body["method"] == "tools/call"
        assert request.headers["mcp-session-id"] == "session-abc"
        return httpx.Response(200, json=_tools_result(body["id"], search_text))

    client = TianyanchaClient(
        api_key="test-api-key", transport=httpx.MockTransport(handler)
    )

    candidates, total = await client.search_candidates("乐视", page=1, page_size=10)

    assert total == 111
    assert candidates[0].name == "乐视网信息技术（北京）股份有限公司"
    assert candidates[0].company_id == "14427175"
    # initialize 只发生一次：后续调用复用会话。
    assert [json.loads(r.content)["method"] for r in requests] == [
        "initialize",
        "tools/call",
    ]
    assert requests[0].headers["authorization"] == "test-api-key"
    assert "mcp-session-id" not in requests[0].headers


async def test_client_sends_authorization_exactly_api_key() -> None:
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers["authorization"])
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(200, json=_tools_result(body["id"], "空结果"))

    client = TianyanchaClient(
        api_key="abc123", transport=httpx.MockTransport(handler)
    )

    await client.search_candidates("x", page=1, page_size=1)

    assert auth_headers == ["abc123", "abc123"]


async def test_invalid_token_maps_to_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(
            401,
            json={
                "error": "invalid_token",
                "error_description": "invalid MCP API key",
            },
        )

    client = TianyanchaClient(
        api_key="bad-token", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(BusinessError) as error:
        await client.search_candidates("x", page=1, page_size=1)

    assert error.value.code == ErrorCode.TIANYANCHA_AUTH_FAILED
    assert error.value.http_status == 502
    assert "bad-token" not in error.value.message


async def test_quota_error_maps_to_quota_exceeded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(402, json={"code": 300007, "message": "quota"})

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(BusinessError) as error:
        await client.search_candidates("x", page=1, page_size=1)

    assert error.value.code == ErrorCode.TIANYANCHA_QUOTA_EXCEEDED
    assert error.value.http_status == 402


async def test_rate_limit_maps_to_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(429, json={"code": 300004})

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(BusinessError) as error:
        await client.search_candidates("x", page=1, page_size=1)

    assert error.value.code == ErrorCode.TIANYANCHA_RATE_LIMITED
    assert error.value.http_status == 429


async def test_server_error_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(503, text="upstream down")

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(BusinessError) as error:
        await client.search_candidates("x", page=1, page_size=1)

    assert error.value.code == ErrorCode.TIANYANCHA_UNAVAILABLE
    assert error.value.http_status == 502


async def test_malformed_body_maps_to_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(200, text="this is not json")

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(BusinessError) as error:
        await client.search_candidates("x", page=1, page_size=1)

    assert error.value.code == ErrorCode.TIANYANCHA_MALFORMED_RESPONSE
    assert error.value.http_status == 502


async def test_sse_response_parses_like_json() -> None:
    search_text = _read_extra("search_companies.md")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        payload = json.dumps(
            _tools_result(body["id"], search_text), ensure_ascii=False
        )
        sse = f"event: message\ndata: {payload}\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
        )

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    candidates, total = await client.search_candidates("乐视", page=1, page_size=10)

    assert total == 111
    assert len(candidates) == 4


async def test_fetch_registration_empty_result_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(
            200,
            json=_tools_result(body["id"], "> 空结果：未发现该维度记录"),
        )

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(BusinessError) as error:
        await client.fetch_registration("91000000000000000X")

    assert error.value.code == ErrorCode.TIANYANCHA_NOT_FOUND
    assert error.value.http_status == 404


async def test_fetch_registration_uses_call_tool_payload() -> None:
    registration_text = _read_extra("registration.md")
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        captured.append(body)
        return httpx.Response(
            200, json=_tools_result(body["id"], registration_text)
        )

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    info = await client.fetch_registration("911100007693890511")

    assert info.credit_code == "911100007693890511"
    assert captured[0]["params"] == {
        "name": "call_tool",
        "arguments": {
            "company_name": "911100007693890511",
            "tool_name": "get_company_registration_info",
            "arguments": {},
        },
    }


async def test_fetch_historical_registration_text_returns_raw_markdown() -> None:
    historical_text = _read_extra("historical_registration_excerpt.md")
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        captured.append(body)
        return httpx.Response(
            200, json=_tools_result(body["id"], historical_text)
        )

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    text = await client.fetch_historical_registration_text("911100007693890511")

    assert text == historical_text
    assert captured[0]["params"]["arguments"]["tool_name"] == (
        "get_historical_registration"
    )


async def test_fetch_historical_registration_empty_result_returns_empty_string() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(
            200,
            json=_tools_result(body["id"], "> 空结果：未发现该维度记录"),
        )

    client = TianyanchaClient(api_key="k", transport=httpx.MockTransport(handler))

    text = await client.fetch_historical_registration_text("91000000000000000X")

    assert text == ""


async def test_connection_invokes_tools_call() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] == "initialize":
            return httpx.Response(
                200, headers={"Mcp-Session-Id": "s"}, json=INITIALIZE_RESPONSE
            )
        return httpx.Response(200, json=_tools_result(body["id"], "空结果"))

    client = TianyanchaClient(
        api_key="k", transport=httpx.MockTransport(handler)
    )

    await client.test_connection()

    assert methods == ["initialize", "tools/call"]
