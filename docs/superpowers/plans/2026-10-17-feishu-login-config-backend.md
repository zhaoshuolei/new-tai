# 飞书登录配置后台化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让管理员通过受独立权限保护的后端 API 安全查看、验证、保存、测试和删除飞书登录配置，并让登录请求实时使用数据库配置或无数据库记录时的环境配置。

**Architecture:** 复用 `integration_config` 表，以 `provider="feishu"` 保存 Fernet 加密的版本化 JSON。新服务负责配置来源解析、凭证验证、保存、测试和删除；登录链路在限流后实时解析有效配置并构造本次请求的飞书客户端。

**Tech Stack:** Python 3.14、FastAPI、Pydantic 2、SQLAlchemy 2 async、asyncmy/MySQL 8、httpx、cryptography/Fernet、pytest、ruff、mypy。

**Spec:** `docs/superpowers/specs/2026-10-17-feishu-login-config-backend-design.md`

## Global Constraints

- 当前仓库只实现后端；不得创建或修改管理前端页面。
- 复用现有 `integration_config` 表，不新增 Alembic 迁移。
- 配置来源优先级固定为：数据库记录存在则覆盖 `.env`；删除数据库记录后才回退 `.env`；都没有时 `source="none"`。
- App Secret 仅允许以 Fernet 密文落库，响应、日志、异常、审计和测试快照中不得出现明文。
- 保存前必须调用 `POST https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal`；只有 `code == 0` 才能写库。
- 首次保存必须提供 App Secret；更新时省略、`null` 或纯空白可沿用当前有效密钥。
- 回调地址允许空字符串；非空时必须是绝对 HTTPS URL，不含 fragment，最大长度 2048。
- 新增权限码只能是 `integration_feishu:read` 和 `integration_feishu:update`。
- 飞书登录接口 `POST /api/v1/auth/feishu/login` 的请求与成功响应结构保持不变。
- 登录必须先执行现有 IP 限流，再解析飞书配置。
- 本地用户名密码应急登录不得受到飞书配置缺失、损坏或不可用的影响。
- 不缓存有效飞书配置或飞书上游 access token。
- 当前工作区已有软删除相关未提交改动，其中 `app/services/auth.py` 是脏文件。执行本计划时不得回滚或混入这些改动；提交飞书相关 hunk 时必须使用 `git add -p` 并只暂存本计划的改动。
- 每个提交只暂存该任务列出的文件；不得使用 `git add .` 或 `git commit -a`。
- Python 代码必须通过 `ruff check`、`ruff format --check` 和 strict `mypy app`。

## Review Focus

- 首次保存不带 App Secret 时必须返回 `1006`、不得调用飞书、不得创建 `provider="feishu"` 行；由 Task 3 的首次保存测试覆盖。
- 数据库配置损坏且请求带新 Secret 时必须允许覆盖修复；不带新 Secret 时必须返回 `8010` 且禁止回退环境配置；由 Task 2 的解析测试和 Task 3 的保存测试覆盖。
- 删除数据库配置后必须立即按环境配置或未配置返回状态，不能继续使用已删除记录；由 Task 3 和 Task 4 覆盖。
- 限流必须先于配置解析；未配置或配置损坏时登录必须明确失败，而本地账号登录不受影响；由 Task 6 覆盖。
- App Secret 绝不能出现在 API 响应、格式化/结构化日志、异常文本或审计记录中；由 Task 2、Task 4 和 Task 7 覆盖。

---

### Task 1: 飞书应用凭证校验与错误码

**Files:**
- Modify: `app/core/errors.py`
- Modify: `app/clients/feishu.py`
- Modify: `tests/test_feishu_login.py`
- Modify: `tests/test_response_and_errors.py`

**Interfaces:**
- Consumes: 现有 `FeishuClient` 的 `httpx.AsyncClient`、`DEFAULT_TIMEOUT`、`_decode_response`。
- Produces: `FEISHU_APP_ACCESS_TOKEN_URL: str`；`FeishuClient.verify_app_credentials() -> None`；`ErrorCode.FEISHU_NOT_CONFIGURED == 8009`、`ErrorCode.FEISHU_CONFIG_UNREADABLE == 8010`、`ErrorCode.FEISHU_CREDENTIALS_REJECTED == 8011`。

- [ ] **Step 1: 写凭证校验失败测试**

在 `tests/test_feishu_login.py` 增加三个异步测试：

```python
async def test_verify_app_credentials_posts_json_and_discards_tokens() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "app_access_token": "must-not-be-returned",
                "tenant_access_token": "must-not-be-returned",
                "expire": 7200,
            },
        )

    client = FeishuClient(
        transport=httpx.MockTransport(handler),
        app_id="cli-test",
        app_secret="secret-value",
    )

    assert await client.verify_app_credentials() is None
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == FEISHU_APP_ACCESS_TOKEN_URL
    assert requests[0].headers["content-type"].startswith("application/json")
    assert json.loads(requests[0].content) == {
        "app_id": "cli-test",
        "app_secret": "secret-value",
    }


async def test_verify_app_credentials_maps_rejection_to_8011() -> None:
    client = FeishuClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"code": 10003, "msg": "no"})
        ),
        app_id="cli-bad",
        app_secret="bad-secret",
    )

    with pytest.raises(BusinessError) as error:
        await client.verify_app_credentials()

    assert error.value.code == ErrorCode.FEISHU_CREDENTIALS_REJECTED
    assert error.value.http_status == 422


async def test_verify_app_credentials_maps_bad_response_to_1011() -> None:
    client = FeishuClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"msg": "missing-code"})
        ),
        app_id="cli-test",
        app_secret="secret-value",
    )

    with pytest.raises(BusinessError) as error:
        await client.verify_app_credentials()

    assert error.value.code == ErrorCode.FEISHU_UPSTREAM_ERROR
    assert error.value.http_status == 502
```

在 `tests/test_response_and_errors.py` 的现有错误码稳定性测试中追加 8009、8010、8011 的断言。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_feishu_login.py::test_verify_app_credentials_posts_json_and_discards_tokens tests/test_feishu_login.py::test_verify_app_credentials_maps_rejection_to_8011 tests/test_feishu_login.py::test_verify_app_credentials_maps_bad_response_to_1011 tests/test_response_and_errors.py::test_customer_and_tianyancha_error_codes_are_stable -v`

Expected: FAIL，原因是 `FEISHU_APP_ACCESS_TOKEN_URL`、`verify_app_credentials` 或新错误码尚不存在。

- [ ] **Step 3: 实现最小客户端方法**

在 `app/core/errors.py` 的第三方集成错误码段加入：

```python
FEISHU_NOT_CONFIGURED = 8009
FEISHU_CONFIG_UNREADABLE = 8010
FEISHU_CREDENTIALS_REJECTED = 8011
```

在 `app/clients/feishu.py` 定义常量，并增加方法：

```python
FEISHU_APP_ACCESS_TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
)


async def verify_app_credentials(self) -> None:
    """验证当前应用凭证；响应中的任何 token 都只丢弃。"""
```

实现要求：POST JSON `{"app_id": ..., "app_secret": ...}`；网络错误、非 JSON、缺少整数 `code` 使用现有 `_raise_upstream_error("verify_app_credentials", ...)` 映射为 `1011`；整数 `code != 0` 抛 `BusinessError(ErrorCode.FEISHU_CREDENTIALS_REJECTED, "Feishu credentials rejected", http_status=422)`；`code == 0` 直接返回 `None`。不得记录请求体或响应 token。

- [ ] **Step 4: 运行测试确认通过**

Run: 与 Step 2 相同

Expected: 4 passed。

- [ ] **Step 5: 运行静态检查**

Run: `uv run ruff check app/clients/feishu.py app/core/errors.py tests/test_feishu_login.py tests/test_response_and_errors.py`

Expected: `All checks passed!`

- [ ] **Step 6: 提交**

```bash
git add app/core/errors.py app/clients/feishu.py tests/test_feishu_login.py tests/test_response_and_errors.py
git commit -m "feat: validate Feishu app credentials"
```

---

### Task 2: 配置模型、来源解析与 Secret 防护

**Files:**
- Create: `app/schemas/integration.py`
- Create: `app/services/feishu_integration.py`
- Create: `tests/test_feishu_integration_service.py`
- Modify: `app/core/crypto.py`
- Modify: `app/core/logging.py`
- Modify: `tests/test_crypto.py`
- Modify: `tests/test_logging_redaction.py`

**Interfaces:**
- Consumes: Task 1 的 `ErrorCode.FEISHU_CONFIG_UNREADABLE`、`ErrorCode.FEISHU_NOT_CONFIGURED`；现有 `settings.feishu_app_id`、`settings.feishu_app_secret`、`settings.feishu_redirect_uri`；现有 `get_by_provider`、`decrypt_secret`。
- Produces: `FeishuConfigSource`；`FeishuConfigPayload`；`FeishuIntegrationUpdate`；`FeishuIntegrationStatus`；`EffectiveFeishuConfig`；`async resolve_feishu_config(session: AsyncSession) -> EffectiveFeishuConfig`；`require_configured_feishu_config(config: EffectiveFeishuConfig) -> EffectiveFeishuConfig`；`async get_feishu_integration_status(session: AsyncSession) -> FeishuIntegrationStatus`。

- [ ] **Step 1: 写模型校验与 Secret 脱敏失败测试**

在 `tests/test_feishu_integration_service.py` 建立数据库 session fixture，并增加：

```python
@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (None, ""),
        ("https://admin.example.com/auth/feishu/callback", "https://admin.example.com/auth/feishu/callback"),
        ("  https://admin.example.com/callback  ", "https://admin.example.com/callback"),
    ],
)
def test_update_normalizes_allowed_redirect_uri(
    redirect_uri: str | None,
    expected: str,
) -> None:
    payload = FeishuIntegrationUpdate(
        app_id="  cli-test  ",
        app_secret="  secret-value  ",
        redirect_uri=redirect_uri,
    )
    assert payload.app_id == "cli-test"
    assert payload.app_secret == "secret-value"
    assert payload.redirect_uri == expected


@pytest.mark.parametrize(
    "redirect_uri",
    ["http://admin.example.com/callback", "/relative", "https://example.com/a#fragment", "https://" + "a" * 2040],
)
def test_update_rejects_invalid_redirect_uri(redirect_uri: str) -> None:
    with pytest.raises(ValidationError):
        FeishuIntegrationUpdate(
            app_id="cli-test",
            app_secret=None,
            redirect_uri=redirect_uri,
        )


def test_update_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FeishuIntegrationUpdate(
            app_id="cli-test",
            app_secret=None,
            unexpected="value",
        )
```

在 `tests/test_logging_redaction.py` 增加结构化、JSON 风格和格式化日志断言，确保 `app_secret=secret-value` 与 `{"app_secret":"secret-value"}` 都只留下 `***`。

在 `tests/test_crypto.py` 增加：

```python
def test_decrypt_secret_can_use_caller_error_code() -> None:
    tampered = _tamper(encrypt_secret("x"))

    with pytest.raises(BusinessError) as error:
        decrypt_secret(
            tampered,
            error_code=ErrorCode.FEISHU_CONFIG_UNREADABLE,
            error_message="Feishu configuration cannot be read",
        )

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert error.value.http_status == 503
```

- [ ] **Step 2: 写来源解析和状态测试**

在 `tests/test_feishu_integration_service.py` 增加：

```python
async def test_resolve_prefers_valid_database_config(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    await add_feishu_config(session, app_id="cli-db", app_secret="db-secret")

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.DATABASE
    assert config.app_id == "cli-db"
    assert config.app_secret == "db-secret"


async def test_resolve_falls_back_to_environment(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", " cli-env ")
    monkeypatch.setattr(settings, "feishu_app_secret", " env-secret ")
    monkeypatch.setattr(settings, "feishu_redirect_uri", "")

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.ENVIRONMENT
    assert config.app_id == "cli-env"
    assert config.app_secret == "env-secret"
    assert config.updated_at is None


async def test_resolve_returns_none_source_when_environment_is_incomplete(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "")

    config = await resolve_feishu_config(session)

    assert config.source is FeishuConfigSource.NONE
    assert config.app_id == ""
    assert config.app_secret == ""


async def test_damaged_database_config_never_falls_back_to_environment(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await resolve_feishu_config(session)

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert error.value.http_status == 503


@pytest.mark.parametrize(
    "plaintext",
    [
        "not-json",
        '{"version":2,"app_id":"cli-x","app_secret":"x","redirect_uri":""}',
        '{"version":1,"app_id":"","app_secret":"x","redirect_uri":""}',
        '{"version":1,"app_id":"cli-x","app_secret":"x","redirect_uri":"http://insecure"}',
        '{"version":1,"app_id":"cli-x","redirect_uri":""}',
    ],
)
async def test_invalid_decrypted_payload_is_unreadable(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    plaintext: str,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext=encrypt_secret(plaintext),
            updated_by=1,
        )
    )
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await resolve_feishu_config(session)

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE


async def test_status_never_contains_app_secret(session: AsyncSession) -> None:
    await add_feishu_config(session, app_secret="must-not-leak")

    status = await get_feishu_integration_status(session)

    assert status.source is FeishuConfigSource.DATABASE
    assert "must-not-leak" not in status.model_dump_json()
```

在同一测试文件增加本地 helper：

```python
async def add_feishu_config(
    session: AsyncSession,
    *,
    app_id: str = "cli-test",
    app_secret: str = "secret-value",
    redirect_uri: str = "https://admin.example.com/auth/feishu/callback",
) -> IntegrationConfig:
    payload = FeishuConfigPayload(
        version=1,
        app_id=app_id,
        app_secret=app_secret,
        redirect_uri=redirect_uri,
    )
    row = IntegrationConfig(
        provider="feishu",
        secret_ciphertext=encrypt_secret(payload.model_dump_json()),
        updated_by=7,
    )
    session.add(row)
    await session.flush()
    return row
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_feishu_integration_service.py tests/test_crypto.py::test_decrypt_secret_can_use_caller_error_code tests/test_logging_redaction.py -v`

Expected: FAIL，原因是新 schema/service/helper、`decrypt_secret` 参数和 `app_secret` 脱敏尚不存在。

- [ ] **Step 4: 实现 schema 和配置解析**

在 `app/schemas/integration.py` 定义：

```python
class FeishuConfigSource(StrEnum):
    DATABASE = "database"
    ENVIRONMENT = "environment"
    NONE = "none"


class FeishuConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    app_id: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=1, max_length=512, repr=False)
    redirect_uri: str = Field(default="", max_length=2048)


class FeishuIntegrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=256)
    app_secret: str | None = Field(default=None, max_length=512, repr=False)
    redirect_uri: str | None = Field(default=None, max_length=2048)


class FeishuIntegrationStatus(BaseModel):
    configured: bool
    source: FeishuConfigSource
    app_id: str | None
    redirect_uri: str | None
    updated_at: datetime | None
    updated_by: int | None
```

为输入和内部模型使用同一个 `field_validator`：`app_id` 去首尾空白且不得为空；`app_secret` 去首尾空白，空白输入规范为 `None`（`FeishuConfigPayload` 仍必须非空）；`redirect_uri=None` 规范为 `""`，非空时用 `urlsplit` 校验 scheme 为 `https`、存在 hostname、无 fragment。`FeishuIntegrationStatus.updated_at` 使用与客户集成状态相同的 UTC ISO JSON serializer。

在 `app/services/feishu_integration.py` 定义：

```python
@dataclass(frozen=True, slots=True)
class EffectiveFeishuConfig:
    source: FeishuConfigSource
    app_id: str
    app_secret: str = field(repr=False)
    redirect_uri: str
    updated_at: datetime | None = None
    updated_by: int | None = None


async def resolve_feishu_config(session: AsyncSession) -> EffectiveFeishuConfig:
    ...


def require_configured_feishu_config(
    config: EffectiveFeishuConfig,
) -> EffectiveFeishuConfig:
    ...


async def get_feishu_integration_status(
    session: AsyncSession,
) -> FeishuIntegrationStatus:
    ...
```

实现要求：数据库行存在时调用 `decrypt_secret(row.secret_ciphertext, error_code=ErrorCode.FEISHU_CONFIG_UNREADABLE, error_message="Feishu configuration cannot be read")`，再用 `FeishuConfigPayload.model_validate_json`；解密、UTF-8、JSON、版本或字段错误全部映射为 8010，不记录载荷内容。数据库不存在时读取 `.env`，App ID 与 Secret 都 trim 后非空才返回 `ENVIRONMENT`，否则返回 `NONE`。`require_configured_feishu_config` 对 `NONE` 抛 `8009` / HTTP 409。

在 `app/core/crypto.py` 把签名扩展为：

```python
def decrypt_secret(
    ciphertext: str,
    *,
    error_code: ErrorCode = ErrorCode.TIANYANCHA_TOKEN_UNREADABLE,
    error_message: str = "Stored credential cannot be read; please re-save it",
) -> str:
    ...
```

保持天眼查默认行为完全不变。

在 `app/core/logging.py` 的 `SENSITIVE_KEYS` 和 `_SENSITIVE_VALUE_PATTERN` 加入 `app_secret`。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_feishu_integration_service.py tests/test_crypto.py tests/test_logging_redaction.py -v`

Expected: PASS。

- [ ] **Step 6: 运行类型与格式检查**

Run: `uv run ruff check app/core/crypto.py app/core/logging.py app/schemas/integration.py app/services/feishu_integration.py tests/test_feishu_integration_service.py tests/test_crypto.py tests/test_logging_redaction.py`

Run: `uv run mypy app`

Expected: 两项均成功。

- [ ] **Step 7: 提交**

```bash
git add app/schemas/integration.py app/services/feishu_integration.py app/core/crypto.py app/core/logging.py tests/test_feishu_integration_service.py tests/test_crypto.py tests/test_logging_redaction.py
git commit -m "feat: resolve encrypted Feishu login configuration"
```

---

### Task 3: 保存、测试与删除服务

**Files:**
- Modify: `app/repositories/integration_config.py`
- Modify: `app/services/feishu_integration.py`
- Modify: `tests/test_feishu_integration_service.py`

**Interfaces:**
- Consumes: Task 1 的 `FeishuClient.verify_app_credentials()`；Task 2 的 schema、`resolve_feishu_config`、`require_configured_feishu_config`；现有 `encrypt_secret`、`upsert`、`@audited`。
- Produces: `async delete_by_provider(session: AsyncSession, provider: str) -> bool`；`build_feishu_client(config: EffectiveFeishuConfig) -> FeishuClient`；`async save_feishu_integration(session: AsyncSession, payload: FeishuIntegrationUpdate, *, context: AuthContext, client: FeishuClient | None = None) -> FeishuIntegrationStatus`；`async test_feishu_connection(session: AsyncSession, *, client: FeishuClient | None = None) -> None`；`async delete_feishu_integration(session: AsyncSession, *, context: AuthContext) -> FeishuIntegrationStatus`。

- [ ] **Step 1: 写首次保存、沿用密钥和失败不覆盖测试**

在 `tests/test_feishu_integration_service.py` 增加：

```python
async def test_first_save_without_secret_fails_before_upstream(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    verify = AsyncMock()
    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    with pytest.raises(BusinessError) as error:
        await save_feishu_integration(
            session,
            FeishuIntegrationUpdate(app_id="cli-new", app_secret=None),
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert verify.await_count == 0
    assert await get_by_provider(session, "feishu") is None


async def test_update_reuses_database_secret(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session, app_secret="old-secret")
    seen: list[tuple[str, str]] = []

    async def verify(self: FeishuClient) -> None:
        seen.append((self._app_id, self._app_secret))

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    status = await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(
            app_id="cli-updated",
            app_secret="   ",
            redirect_uri="https://admin.example.com/new-callback",
        ),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    stored = FeishuConfigPayload.model_validate_json(decrypt_secret(row.secret_ciphertext))
    assert seen == [("cli-updated", "old-secret")]
    assert stored.app_secret == "old-secret"
    assert status.app_id == "cli-updated"
    assert "old-secret" not in status.model_dump_json()


async def test_removed_app_id_with_reused_secret_does_not_replace_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await add_feishu_config(session, app_id="cli-old", app_secret="old-secret")
    old_ciphertext = row.secret_ciphertext
    seen: list[tuple[str, str]] = []

    async def reject(self: FeishuClient) -> None:
        seen.append((self._app_id, self._app_secret))
        raise BusinessError(
            ErrorCode.FEISHU_CREDENTIALS_REJECTED,
            "Feishu credentials rejected",
            http_status=422,
        )

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", reject)

    with pytest.raises(BusinessError) as error:
        await save_feishu_integration(
            session,
            FeishuIntegrationUpdate(app_id="cli-new", app_secret=None),
            context=context,
        )

    await session.refresh(row)
    assert seen == [("cli-new", "old-secret")]
    assert error.value.code == ErrorCode.FEISHU_CREDENTIALS_REJECTED
    assert row.secret_ciphertext == old_ciphertext
```

- [ ] **Step 2: 写损坏配置修复、环境密钥沿用和存储测试**

继续增加：

```python
async def test_new_secret_can_repair_damaged_database_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    status = await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(app_id="cli-repaired", app_secret="new-secret"),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    stored = FeishuConfigPayload.model_validate_json(decrypt_secret(row.secret_ciphertext))
    assert status.source is FeishuConfigSource.DATABASE
    assert stored.app_id == "cli-repaired"
    assert stored.app_secret == "new-secret"


async def test_update_without_secret_cannot_repair_damaged_database_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    verify = AsyncMock()
    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    with pytest.raises(BusinessError) as error:
        await save_feishu_integration(
            session,
            FeishuIntegrationUpdate(app_id="cli-new", app_secret=None),
            context=context,
        )

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert verify.await_count == 0


async def test_update_without_database_row_can_reuse_environment_secret(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(app_id="cli-db", app_secret=None),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    stored = FeishuConfigPayload.model_validate_json(decrypt_secret(row.secret_ciphertext))
    assert stored.app_secret == "env-secret"


async def test_saved_payload_is_encrypted_and_not_plaintext(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(
            app_id="cli-secure",
            app_secret="very-secret-value",
            redirect_uri="https://admin.example.com/callback",
        ),
        context=context,
    )

    row = await get_by_provider(session, "feishu")
    assert "very-secret-value" not in row.secret_ciphertext
    assert "cli-secure" not in row.secret_ciphertext
```

- [ ] **Step 3: 写测试与删除语义测试**

增加：

```python
async def test_test_connection_uses_effective_database_config(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session, app_id="cli-db", app_secret="db-secret")
    seen: list[tuple[str, str]] = []

    async def verify(self: FeishuClient) -> None:
        seen.append((self._app_id, self._app_secret))

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", verify)

    await test_feishu_connection(session)

    assert seen == [("cli-db", "db-secret")]


async def test_delete_reveals_environment_config(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session)
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")

    status = await delete_feishu_integration(session, context=context)

    assert await get_by_provider(session, "feishu") is None
    assert status.configured is True
    assert status.source is FeishuConfigSource.ENVIRONMENT
    assert status.app_id == "cli-env"


async def test_repeated_delete_is_idempotent(
    session: AsyncSession,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")

    first = await delete_feishu_integration(session, context=context)
    second = await delete_feishu_integration(session, context=context)

    assert first.source is FeishuConfigSource.NONE
    assert second.source is FeishuConfigSource.NONE
    assert second.configured is False


async def test_test_connection_rejects_unconfigured_and_damaged_sources(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    with pytest.raises(BusinessError) as missing:
        await test_feishu_connection(session)
    assert missing.value.code == ErrorCode.FEISHU_NOT_CONFIGURED

    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    with pytest.raises(BusinessError) as damaged:
        await test_feishu_connection(session)
    assert damaged.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/test_feishu_integration_service.py -v`

Expected: FAIL，原因是保存、测试、删除服务和 repository 删除函数尚不存在。

- [ ] **Step 5: 实现 service 与 repository**

在 `app/repositories/integration_config.py` 增加：

```python
async def delete_by_provider(
    session: AsyncSession,
    provider: str,
) -> bool:
    result = await session.execute(
        delete(IntegrationConfig).where(IntegrationConfig.provider == provider)
    )
    return bool(getattr(result, "rowcount", 0) or 0)
```

在 `app/services/feishu_integration.py` 增加 `_PROVIDER = "feishu"` 和：

```python
def build_feishu_client(config: EffectiveFeishuConfig) -> FeishuClient:
    return FeishuClient(
        app_id=config.app_id,
        app_secret=config.app_secret,
        redirect_uri=config.redirect_uri or None,
    )


@audited(action="integration_feishu:update", resource_type="integration")
async def save_feishu_integration(
    session: AsyncSession,
    payload: FeishuIntegrationUpdate,
    *,
    context: AuthContext,
    client: FeishuClient | None = None,
) -> FeishuIntegrationStatus:
    ...


async def test_feishu_connection(
    session: AsyncSession,
    *,
    client: FeishuClient | None = None,
) -> None:
    ...


@audited(action="integration_feishu:update", resource_type="integration")
async def delete_feishu_integration(
    session: AsyncSession,
    *,
    context: AuthContext,
) -> FeishuIntegrationStatus:
    ...
```

保存实现必须遵守：

1. `requested_secret = payload.app_secret`；它已由 schema trim，空白为 `None`。
2. 若无新 Secret，只调用一次 `resolve_feishu_config` 并 `require_configured_feishu_config`；捕获其 8009 并转换为 `1006` / HTTP 422，损坏的 8010 原样上抛。
3. 若有新 Secret，不读取旧配置，确保损坏记录可被完整新配置修复。
4. 候选客户端为 `client or FeishuClient(app_id=payload.app_id, app_secret=final_secret)`，调用 `verify_app_credentials()`。
5. 验证成功后构造 `FeishuConfigPayload(version=1, app_id=payload.app_id, app_secret=final_secret, redirect_uri=payload.redirect_uri)`，加密 `model_dump_json()`，使用 `upsert(..., updated_by=context.user_id)`，再 `commit()`。
6. 返回 `FeishuIntegrationStatus(configured=True, source=DATABASE, app_id=row payload app_id, redirect_uri=..., updated_at=row.updated_at, updated_by=row.updated_by)`。不得从内存明文模型直接返回 Secret。

测试实现：解析并 require 当前配置，使用 `client or build_feishu_client(config)` 调用 `verify_app_credentials()`。

删除实现：调用 `delete_by_provider`，无论是否删除都 `commit()`，随后返回 `get_feishu_integration_status(session)`。

- [ ] **Step 6: 运行服务测试确认通过**

Run: `uv run pytest tests/test_feishu_integration_service.py -v`

Expected: PASS。

- [ ] **Step 7: 静态检查并提交**

Run: `uv run ruff check app/repositories/integration_config.py app/services/feishu_integration.py tests/test_feishu_integration_service.py`

```bash
git add app/repositories/integration_config.py app/services/feishu_integration.py tests/test_feishu_integration_service.py
git commit -m "feat: manage Feishu login configuration"
```

---

### Task 4: 飞书配置 API

**Files:**
- Modify: `app/api/integrations.py`
- Create: `tests/test_feishu_integration_api.py`

**Interfaces:**
- Consumes: Task 2 和 Task 3 的 schema 与 service 函数。
- Produces: `GET /api/v1/integrations/feishu`、`PUT /api/v1/integrations/feishu`、`POST /api/v1/integrations/feishu/test`、`DELETE /api/v1/integrations/feishu`。

- [ ] **Step 1: 写 API 契约和权限测试**

在 `tests/test_feishu_integration_api.py` 建立独立的 session、audit factory、context override 和 AsyncClient fixture。增加：

```python
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/integrations/feishu"),
        ("put", "/api/v1/integrations/feishu"),
        ("post", "/api/v1/integrations/feishu/test"),
        ("delete", "/api/v1/integrations/feishu"),
    ],
)
async def test_feishu_config_requires_permissions(
    api_client_factory,
    method: str,
    path: str,
) -> None:
    async with api_client_factory(permissions=()) as client:
        response = await getattr(client, method)(
            path,
            json={"app_id": "cli-test", "app_secret": "secret"} if method == "put" else None,
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/api/v1/integrations/feishu"),
        ("post", "/api/v1/integrations/feishu/test"),
        ("delete", "/api/v1/integrations/feishu"),
    ],
)
async def test_read_permission_cannot_mutate_or_probe(
    api_client_factory,
    method: str,
    path: str,
) -> None:
    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await getattr(client, method)(
            path,
            json={"app_id": "cli-test", "app_secret": "secret"} if method == "put" else None,
        )
    assert response.status_code == 403
```

`api_client_factory` 的 fixture 必须为每次调用创建独立 app，并覆盖 `get_session` 与 `get_current_context`；默认先把 `settings.feishu_app_id` 和 `settings.feishu_app_secret` 清空，避免测试依赖开发机 `.env`。

再增加读取与保存成功测试：

```python
async def test_get_returns_source_without_secret(api_client_factory, session) -> None:
    await add_feishu_config(session, app_secret="must-not-leak")
    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.get("/api/v1/integrations/feishu")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "database"
    assert data["app_id"] == "cli-test"
    assert "must-not-leak" not in response.text
    assert "app_secret" not in response.text


async def test_put_saves_and_returns_database_status(
    api_client_factory,
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            "/api/v1/integrations/feishu",
            json={
                "app_id": "cli-api",
                "app_secret": "api-secret",
                "redirect_uri": "https://admin.example.com/callback",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["source"] == "database"
    assert response.json()["data"]["app_id"] == "cli-api"
    assert "api-secret" not in response.text
```

增加未配置、测试、删除和损坏映射测试：

```python
async def test_get_reports_none_source(api_client_factory) -> None:
    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.get("/api/v1/integrations/feishu")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "configured": False,
        "source": "none",
        "app_id": None,
        "redirect_uri": None,
        "updated_at": None,
        "updated_by": None,
    }


async def test_first_save_without_secret_returns_1006(
    api_client_factory,
) -> None:
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            "/api/v1/integrations/feishu",
            json={"app_id": "cli-missing-secret"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.INVALID_PARAMETER


async def test_test_and_delete_require_effective_config(
    api_client_factory,
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        missing = await client.post("/api/v1/integrations/feishu/test")
    assert missing.status_code == 409
    assert missing.json()["code"] == ErrorCode.FEISHU_NOT_CONFIGURED

    await add_feishu_config(session)
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        tested = await client.post("/api/v1/integrations/feishu/test")
        deleted = await client.delete("/api/v1/integrations/feishu")
    assert tested.status_code == 200
    assert deleted.status_code == 200
    assert deleted.json()["data"]["source"] == "none"


async def test_get_maps_damaged_config_to_8010(
    api_client_factory,
    session,
) -> None:
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()

    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        response = await client.get("/api/v1/integrations/feishu")

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.FEISHU_CONFIG_UNREADABLE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_feishu_integration_api.py -v`

Expected: FAIL，四个 API 路径均为 404。

- [ ] **Step 3: 实现 API 路由**

在 `app/api/integrations.py` 增加：

```python
@router.get(
    "/feishu",
    response_model=SuccessEnvelope[FeishuIntegrationStatus],
    dependencies=[Depends(require_permission("integration_feishu:read"))],
)
async def get_feishu_integration(...):
    ...


@router.put(
    "/feishu",
    response_model=SuccessEnvelope[FeishuIntegrationStatus],
    dependencies=[Depends(require_permission("integration_feishu:update"))],
)
async def update_feishu_integration(...):
    ...


@router.post(
    "/feishu/test",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("integration_feishu:update"))],
)
async def test_feishu_integration(...):
    ...


@router.delete(
    "/feishu",
    response_model=SuccessEnvelope[FeishuIntegrationStatus],
    dependencies=[Depends(require_permission("integration_feishu:update"))],
)
async def delete_feishu_integration(...):
    ...
```

返回规则：GET 直接返回状态；PUT 返回状态并用 `message="updated"`；POST test 返回 `success(message="ok")`；DELETE 返回状态并用 `message="deleted"`。保存和删除传 `context`，GET 与 test 的可注入 context 参数命名为 `_context`。服务返回的 Pydantic model 直接传给 `success`。

在文件底部注册：

```python
register_audit_route(
    "PUT",
    "/integrations/feishu",
    action="integration_feishu:update",
    resource_type="integration",
)
register_audit_route(
    "DELETE",
    "/integrations/feishu",
    action="integration_feishu:update",
    resource_type="integration",
)
```

- [ ] **Step 4: 运行 API 测试确认通过**

Run: `uv run pytest tests/test_feishu_integration_api.py -v`

Expected: PASS。

- [ ] **Step 5: 验证 OpenAPI 与静态检查**

Run: `uv run pytest tests/test_response_and_errors.py::test_business_routes_expose_success_envelope_in_openapi -v`

Run: `uv run ruff check app/api/integrations.py tests/test_feishu_integration_api.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/api/integrations.py tests/test_feishu_integration_api.py
git commit -m "feat: expose Feishu configuration APIs"
```

---

### Task 5: 独立权限与菜单种子

**Files:**
- Modify: `scripts/init_data.py`
- Modify: `tests/test_seed_data.py`

**Interfaces:**
- Consumes: Task 4 的权限码。
- Produces: 系统管理下的“飞书登录配置”菜单和“保存飞书配置”按钮；权限码 `integration_feishu:read`、`integration_feishu:update`。

- [ ] **Step 1: 写菜单种子失败测试**

在 `tests/test_seed_data.py` 的 `EXPECTED_PERMISSION_CODES` 加入两个新码，并增加：

```python
async def test_feishu_integration_menu_and_button_are_seeded(
    session: AsyncSession,
) -> None:
    await init_data(
        session,
        superadmin_username="superadmin",
        superadmin_password="test-password1",
    )

    menu = await session.scalar(
        select(Menu).where(Menu.permission_code == "integration_feishu:read")
    )
    button = await session.scalar(
        select(Menu).where(Menu.permission_code == "integration_feishu:update")
    )

    assert menu is not None
    assert menu.type == MenuType.MENU
    assert menu.path == "/system/integrations/feishu"
    assert button is not None
    assert button.type == MenuType.BUTTON
    assert button.parent_id == menu.id
```

在 `test_default_role_has_no_customer_permissions` 的禁止集合加入两个新码，或把测试改名为 `test_default_role_has_no_administration_permissions` 后保留原断言。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_seed_data.py::test_feishu_integration_menu_and_button_are_seeded tests/test_seed_data.py::test_init_creates_baseline_menu_tree tests/test_seed_data.py::test_default_role_has_no_customer_permissions -v`

Expected: FAIL，原因是种子菜单与新权限码不存在。

- [ ] **Step 3: 添加菜单种子**

在 `scripts/init_data.py` 的天眼查菜单后加入：

```python
_MenuSeed(
    key="integration-feishu",
    parent_key="system",
    menu_type=MenuType.MENU,
    name="飞书登录配置",
    path="/system/integrations/feishu",
    permission_code="integration_feishu:read",
    sort_order=90,
),
_MenuSeed(
    key="integration-feishu-update",
    parent_key="integration-feishu",
    menu_type=MenuType.BUTTON,
    name="保存飞书配置",
    path=None,
    permission_code="integration_feishu:update",
    sort_order=10,
),
```

- [ ] **Step 4: 运行种子测试确认通过**

Run: `uv run pytest tests/test_seed_data.py -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add scripts/init_data.py tests/test_seed_data.py
git commit -m "feat: seed Feishu integration permissions"
```

---

### Task 6: 登录链路实时使用有效配置

**Files:**
- Modify: `app/api/auth.py`
- Modify: `app/services/auth.py`
- Modify: `tests/test_feishu_login.py`
- Modify: `tests/test_login_log.py`
- Create: `tests/feishu_helpers.py`

**Interfaces:**
- Consumes: Task 2 的 `resolve_feishu_config`、`require_configured_feishu_config`；Task 3 的 `build_feishu_client`。
- Produces: 登录在限流后解析配置；无显式 `client` 时用本次有效配置构造 `FeishuClient`；保留 `login_with_feishu(..., client=...)` 作为单元测试注入点。

- [ ] **Step 1: 建共享测试配置 helper**

创建 `tests/feishu_helpers.py`：

```python
async def add_feishu_config(
    session: AsyncSession,
    *,
    app_id: str = "cli-test",
    app_secret: str = "secret-value",
    redirect_uri: str = "https://admin.example.com/auth/feishu/callback",
    updated_by: int = 7,
) -> IntegrationConfig:
    payload = FeishuConfigPayload(
        version=1,
        app_id=app_id,
        app_secret=app_secret,
        redirect_uri=redirect_uri,
    )
    row = IntegrationConfig(
        provider="feishu",
        secret_ciphertext=encrypt_secret(payload.model_dump_json()),
        updated_by=updated_by,
    )
    session.add(row)
    await session.flush()
    return row
```

把 Task 2 测试内的同名本地 helper 改为从该文件导入，确保所有测试共用同一编码格式。

- [ ] **Step 2: 写登录来源、缺失、损坏和限流顺序测试**

在 `tests/test_feishu_login.py` 增加 `configured_feishu` fixture，并在所有执行真实登录路径的现有测试参数中加入它：

```python
@pytest_asyncio.fixture
async def configured_feishu(session: AsyncSession) -> IntegrationConfig:
    return await add_feishu_config(session)
```

增加：

```python
async def test_login_resolves_database_config_after_rate_limit(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    fake = FakeFeishuClient(open_id="ou-db-config")
    seen: list[EffectiveFeishuConfig] = []

    def build(config: EffectiveFeishuConfig) -> FeishuClient:
        seen.append(config)
        return fake

    monkeypatch.setattr(auth_service, "build_feishu_client", build)

    await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.30",
    )

    assert len(seen) == 1
    assert seen[0].source is FeishuConfigSource.DATABASE
    assert seen[0].app_id == "cli-test"
    assert seen[0].app_secret == "secret-value"


async def test_login_without_config_returns_8009_before_upstream(
    session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    client = FakeFeishuClient(open_id="ou-no-config")

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.31",
            client=client,
        )

    assert error.value.code == ErrorCode.FEISHU_NOT_CONFIGURED
    assert client.exchange_calls == 0


async def test_login_with_damaged_database_config_returns_8010(
    session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "cli-env")
    monkeypatch.setattr(settings, "feishu_app_secret", "env-secret")
    session.add(
        IntegrationConfig(
            provider="feishu",
            secret_ciphertext="not-a-fernet-token",
            updated_by=1,
        )
    )
    await session.flush()
    client = FakeFeishuClient(open_id="ou-damaged-config")

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.32",
            client=client,
        )

    assert error.value.code == ErrorCode.FEISHU_CONFIG_UNREADABLE
    assert client.exchange_calls == 0


async def test_rate_limit_precedes_config_resolution(
    session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_ip = "203.0.113.34"
    for _ in range(auth_service.LOGIN_RATE_LIMIT_ATTEMPTS):
        await auth_service.enforce_login_ip_rate_limit(fake_redis, client_ip=client_ip)
    resolve = AsyncMock(side_effect=AssertionError("resolver must not run"))
    monkeypatch.setattr(auth_service, "resolve_feishu_config", resolve)

    with pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip=client_ip,
        )

    assert error.value.code == ErrorCode.RATE_LIMITED
    assert resolve.await_count == 0
```

扩展 `FakeFeishuClient`，记录 `exchange_calls` 与 `info_calls`，供上述断言使用。

更新 `tests/test_login_log.py` 的三个飞书登录测试，显式调用 `await add_feishu_config(session)`，保留现有登录日志断言；从 `app.core.config` 导入 `settings`，并从 `tests.feishu_helpers` 导入 helper。再增加：

```python
async def test_feishu_missing_config_records_failure(
    session: AsyncSession,
    fake_redis: Any,
    login_log_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    request_id = unique_request_id()

    with request_context(request_id), pytest.raises(BusinessError) as error:
        await auth_service.login_with_feishu(
            session,
            fake_redis,
            "valid-code",
            client_ip="203.0.113.35",
        )

    assert error.value.code == ErrorCode.FEISHU_NOT_CONFIGURED
    rows = await login_rows(login_log_factory, request_id=request_id)
    assert len(rows) == 1
    assert rows[0].login_type is LoginType.FEISHU
    assert rows[0].result is LoginResultEnum.FAILURE
    assert rows[0].reason == ErrorCode.FEISHU_NOT_CONFIGURED.name
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_feishu_login.py -v`

Expected: FAIL，因为登录仍使用 API 注入的 `.env` 客户端，未解析新配置。

- [ ] **Step 4: 改为服务层解析并构造客户端**

在 `app/services/auth.py` 导入：

```python
from app.services.feishu_integration import (
    build_feishu_client,
    require_configured_feishu_config,
    resolve_feishu_config,
)
```

在 `_login_with_feishu` 中保持顺序：

```python
await enforce_login_ip_rate_limit(redis, client_ip=client_ip)
config = require_configured_feishu_config(await resolve_feishu_config(session))
feishu_client = client or build_feishu_client(config)
user_access_token = await feishu_client.exchange_code(code)
```

不得提前解析配置，不得在模块级缓存 `config`。显式 `client` 仍用于现有服务测试，但配置必须先成功解析。

在 `app/api/auth.py` 删除：

```python
from app.clients.feishu import FeishuClient, get_feishu_client
```

删除 `feishu_login` 的 `feishu_client` 参数及 `client=feishu_client` 实参，保留 `session`、`redis` 和现有响应转换。

在 `tests/test_feishu_login.py` 删除 `get_feishu_client` import 和 dependency override。把 `client` fixture 改为依赖 `configured_feishu`，并注入 API 路径的假客户端：

```python
@pytest_asyncio.fixture
async def client(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    application = create_app()
    feishu_client = FakeFeishuClient(open_id="ou-api-login")
    monkeypatch.setattr(
        auth_service,
        "build_feishu_client",
        lambda _config: feishu_client,
    )
    ...
```

`test_feishu_login_rate_limit_precedes_upstream_exchange` 的自建 app/client 同样依赖 `configured_feishu` 并 monkeypatch `auth_service.build_feishu_client`。所有 service-level 登录测试保留显式 `client=`，同时请求 `configured_feishu` 让配置解析成功。

- [ ] **Step 5: 增加保存后立即生效测试**

在 `tests/test_feishu_login.py` 增加：

```python
async def test_saved_config_is_used_by_next_login_without_restart(
    session: AsyncSession,
    fake_redis: Any,
    configured_feishu: IntegrationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFeishuClient(open_id="ou-after-save")
    seen: list[EffectiveFeishuConfig] = []
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        auth_service,
        "build_feishu_client",
        lambda config: seen.append(config) or fake,
    )
    context = AuthContext(
        user_id=7,
        role_id=None,
        department_id=None,
        data_scope=None,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset({"integration_feishu:update"}),
        is_super_admin=False,
    )

    await save_feishu_integration(
        session,
        FeishuIntegrationUpdate(app_id="cli-new", app_secret="new-secret"),
        context=context,
    )
    await auth_service.login_with_feishu(
        session,
        fake_redis,
        "valid-code",
        client_ip="203.0.113.33",
    )

    assert seen[-1].app_id == "cli-new"
    assert seen[-1].app_secret == "new-secret"
```

- [ ] **Step 6: 运行登录相关测试确认通过**

Run: `uv run pytest tests/test_feishu_login.py tests/test_login_log.py tests/test_local_auth.py -v`

Expected: PASS，尤其包含“缺少配置 8009”“损坏配置 8010”“数据库覆盖环境”“保存后下一次登录生效”和全部本地登录测试。

- [ ] **Step 7: 静态检查并提交**

Run: `uv run ruff check app/api/auth.py app/services/auth.py tests/test_feishu_login.py tests/test_login_log.py tests/feishu_helpers.py`

Run: `uv run mypy app`

注意：`app/services/auth.py` 已有用户软删除改动。使用 `git add -p app/services/auth.py` 只暂存飞书配置解析相关 hunk。

```bash
git add app/api/auth.py tests/test_feishu_login.py tests/test_login_log.py tests/feishu_helpers.py
git add -p app/services/auth.py
git commit -m "feat: load Feishu login config at runtime"
```

---

### Task 7: 审计、安全回归与完整验证

**Files:**
- Modify: `tests/test_feishu_integration_api.py`
- Modify: `tests/test_feishu_integration_service.py`
- Modify: `tests/test_logging_redaction.py`
- Modify if needed: `app/services/feishu_integration.py`
- Modify if needed: `app/api/integrations.py`

**Interfaces:**
- Consumes: 前六个任务全部实现。
- Produces: 保存/删除成功与权限拒绝审计、测试接口不写审计、Secret 全链路不泄漏的可执行证据。

- [ ] **Step 1: 写审计和权限边界测试**

在 `tests/test_feishu_integration_api.py` 增加：

```python
async def test_put_success_records_audit_without_secret(
    api_client_factory,
    audit_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        response = await client.put(
            "/api/v1/integrations/feishu",
            json={"app_id": "cli-audit", "app_secret": "audit-secret"},
        )

    assert response.status_code == 200
    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 1
    assert rows[0].action == "integration_feishu:update"
    assert rows[0].resource_type == "integration"
    assert rows[0].result is AuditResult.SUCCESS
    assert "audit-secret" not in repr(rows[0].__dict__)


async def test_delete_is_audited_even_when_idempotent(
    api_client_factory,
    audit_session_factory,
) -> None:
    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        first = await client.delete("/api/v1/integrations/feishu")
        second = await client.delete("/api/v1/integrations/feishu")

    assert first.status_code == 200
    assert second.status_code == 200
    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 2
    assert all(row.result is AuditResult.SUCCESS for row in rows)


async def test_permission_denial_is_audited_but_test_probe_is_not(
    api_client_factory,
    audit_session_factory,
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_feishu_config(session)
    monkeypatch.setattr(
        FeishuClient,
        "verify_app_credentials",
        AsyncMock(return_value=None),
    )

    async with api_client_factory(permissions=("integration_feishu:read",)) as client:
        denied = await client.put(
            "/api/v1/integrations/feishu",
            json={"app_id": "cli-denied", "app_secret": "denied-secret"},
        )
    assert denied.status_code == 403

    async with api_client_factory(permissions=("integration_feishu:update",)) as client:
        tested = await client.post("/api/v1/integrations/feishu/test")
    assert tested.status_code == 200

    rows = await audit_rows(audit_session_factory)
    assert len(rows) == 1
    assert rows[0].result is AuditResult.DENIED
    assert rows[0].reason == "FORBIDDEN"
```

如果现有 `record_exception` 在 5xx 测试中访问数据库，沿用现有测试 fixture 的 audit/session monkeypatch 模式，不新增产品逻辑。

- [ ] **Step 2: 写跨 API、异常和日志的 Secret 防泄漏测试**

增加：

```python
async def test_save_errors_and_logs_never_contain_secret(
    api_client_factory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "never-log-this-app-secret"

    async def reject(_self: FeishuClient) -> None:
        raise BusinessError(
            ErrorCode.FEISHU_CREDENTIALS_REJECTED,
            "Feishu credentials rejected",
            http_status=422,
        )

    monkeypatch.setattr(FeishuClient, "verify_app_credentials", reject)
    with caplog.at_level("DEBUG"):
        async with api_client_factory(
            permissions=("integration_feishu:update",)
        ) as client:
            response = await client.put(
                "/api/v1/integrations/feishu",
                json={"app_id": "cli-bad", "app_secret": secret},
            )

    assert response.status_code == 422
    assert secret not in response.text
    assert secret not in caplog.text
```

在 `tests/test_logging_redaction.py` 增加 `app_secret` 的结构化、JSON 和格式化三类断言。

- [ ] **Step 3: 运行新增测试确认通过或暴露实现缺口**

Run: `uv run pytest tests/test_feishu_integration_api.py tests/test_feishu_integration_service.py tests/test_logging_redaction.py -v`

Expected: PASS。若失败，只修复审计注册、服务装饰器、日志脱敏或响应模型，不放宽任何 Secret 断言。

- [ ] **Step 4: 运行飞书功能完整回归**

Run: `uv run pytest tests/test_feishu_login.py tests/test_login_log.py tests/test_local_auth.py tests/test_feishu_integration_service.py tests/test_feishu_integration_api.py tests/test_seed_data.py tests/test_logging_redaction.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 运行全项目验证**

Run: `uv run ruff check app tests scripts`

Run: `uv run ruff format --check app tests scripts`

Run: `uv run mypy app`

Run: `uv run pytest -v`

Expected: 四项均成功。若全量测试受现有 MySQL/Redis 环境影响，记录具体失败命令和环境原因；不得声称通过。

- [ ] **Step 6: 审查最终 diff**

Run: `git status --short`

Run: `git diff --check`

Run: `git diff -- app/core/errors.py app/clients/feishu.py app/core/crypto.py app/core/logging.py app/schemas/integration.py app/services/feishu_integration.py app/repositories/integration_config.py app/api/integrations.py app/api/auth.py app/services/auth.py scripts/init_data.py tests/feishu_helpers.py tests/test_feishu_login.py tests/test_login_log.py tests/test_feishu_integration_service.py tests/test_feishu_integration_api.py tests/test_seed_data.py tests/test_logging_redaction.py tests/test_crypto.py tests/test_response_and_errors.py`

Expected: diff 只包含本计划改动；确认没有迁移、前端文件、Secret 明文或用户已有的软删除改动被错误暂存。

- [ ] **Step 7: 提交最终测试**

```bash
git add tests/test_feishu_integration_api.py tests/test_feishu_integration_service.py tests/test_logging_redaction.py
git commit -m "test: cover Feishu configuration security"
```

---

## Self-Review

- **Spec coverage:** 配置来源、优先级、保存验证、Secret 沿用、损坏修复、删除回退、四类 API、独立权限/菜单、登录实时读取、审计、错误码、日志脱敏和测试策略均有对应任务。
- **Review Focus coverage:** 五项高风险输入分别落在 Task 2、Task 3、Task 4、Task 6、Task 7 的明确测试中。
- **Type consistency:** 计划和规格统一使用 `FeishuConfigSource`、`EffectiveFeishuConfig`、`FeishuIntegrationStatus`、`resolve_feishu_config`、`build_feishu_client`；没有使用同义但不同名的接口。
- **No migration:** 所有存储行为复用 `integration_config`，没有 Alembic 步骤。
- **Dirty worktree:** 唯一已知需 hunk 级暂存的重叠文件是 `app/services/auth.py`，已在全局约束和 Task 6 中明确。
