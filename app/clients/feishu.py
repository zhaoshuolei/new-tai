"""飞书开放平台客户端。"""

import logging
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode

FEISHU_APP_ACCESS_TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
)
FEISHU_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeishuUserInfo:
    open_id: str
    name: str
    avatar_url: str | None
    email: str | None


def _raise_upstream_error(operation: str, detail: object | None = None) -> NoReturn:
    logger.warning(
        "Feishu upstream request failed",
        extra={"operation": operation, "detail": detail},
    )
    raise BusinessError(
        ErrorCode.FEISHU_UPSTREAM_ERROR,
        "Feishu authentication service unavailable",
        http_status=502,
    )


def _decode_response(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        _raise_upstream_error(
            operation,
            f"HTTP {response.status_code} returned invalid JSON",
        )

    if not isinstance(payload, dict):
        _raise_upstream_error(operation, "response body is not an object")
    return payload


def _require_success(payload: dict[str, Any], operation: str) -> None:
    code = payload.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        _raise_upstream_error(operation, "response is missing integer code")
    if code != 0:
        _raise_upstream_error(operation, code)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class FeishuClient:
    """调用飞书 OAuth 与用户信息接口。"""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        app_id: str | None = None,
        app_secret: str | None = None,
        redirect_uri: str | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = (
            timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        )
        self._app_id = settings.feishu_app_id if app_id is None else app_id
        self._app_secret = (
            settings.feishu_app_secret if app_secret is None else app_secret
        )
        self._redirect_uri = (
            settings.feishu_redirect_uri if redirect_uri is None else redirect_uri
        )

    async def verify_app_credentials(self) -> None:
        """验证当前应用凭证；响应中的任何 token 都只丢弃。"""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    FEISHU_APP_ACCESS_TOKEN_URL,
                    json={
                        "app_id": self._app_id,
                        "app_secret": self._app_secret,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as error:
            _raise_upstream_error("verify_app_credentials", type(error).__name__)

        payload = _decode_response(response, "verify_app_credentials")
        code = payload.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            _raise_upstream_error(
                "verify_app_credentials",
                "response is missing integer code",
            )
        if code != 0:
            raise BusinessError(
                ErrorCode.FEISHU_CREDENTIALS_REJECTED,
                "Feishu credentials rejected",
                http_status=422,
            )

    async def exchange_code(self, code: str) -> str:
        body = {
            "grant_type": "authorization_code",
            "client_id": self._app_id,
            "client_secret": self._app_secret,
            "code": code,
        }
        if self._redirect_uri:
            body["redirect_uri"] = self._redirect_uri

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    FEISHU_TOKEN_URL,
                    data=body,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as error:
            _raise_upstream_error("exchange_code", type(error).__name__)

        payload = _decode_response(response, "exchange_code")
        _require_success(payload, "exchange_code")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            _raise_upstream_error("exchange_code", "response is missing access_token")
        return access_token

    async def get_user_info(self, user_access_token: str) -> FeishuUserInfo:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    FEISHU_USER_INFO_URL,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {user_access_token}",
                    },
                )
        except httpx.HTTPError as error:
            _raise_upstream_error("get_user_info", type(error).__name__)

        payload = _decode_response(response, "get_user_info")
        _require_success(payload, "get_user_info")
        data = payload.get("data")
        if not isinstance(data, dict):
            _raise_upstream_error("get_user_info", "response is missing data")

        open_id = data.get("open_id")
        name = data.get("name")
        if not isinstance(open_id, str) or not open_id:
            _raise_upstream_error("get_user_info", "response is missing open_id")
        if not isinstance(name, str) or not name:
            _raise_upstream_error("get_user_info", "response is missing name")

        return FeishuUserInfo(
            open_id=open_id,
            name=name,
            avatar_url=_optional_string(data.get("avatar_url")),
            email=_optional_string(data.get("email")),
        )


def get_feishu_client() -> FeishuClient:
    return FeishuClient()


__all__: tuple[str, ...] = (
    "DEFAULT_TIMEOUT",
    "FEISHU_APP_ACCESS_TOKEN_URL",
    "FEISHU_TOKEN_URL",
    "FEISHU_USER_INFO_URL",
    "FeishuClient",
    "FeishuUserInfo",
    "get_feishu_client",
)
