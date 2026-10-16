# 客户管理与天眼查企业信息回填 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为内部后台增加客户管理：业务人员输入简称检索天眼查候选，人工确认后一键回填工商信息（含完整经营范围）；天眼查 token 由超管在后台加密保存。

**Architecture:** 沿用 `api → services → repositories → models` 分层。天眼查 MCP 调用封装在 `app/clients/tianyancha.py`（httpx + JSON-RPC，Markdown 表格解析为纯函数）。token 用 Fernet 加密存入 `integration_config`，密钥来自必填环境变量 `CONFIG_ENCRYPTION_KEY`。客户数据范围复用抽象后的统一过滤函数，与用户共用。

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, asyncmy (MySQL 8), Alembic, Redis, httpx, cryptography (Fernet), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-16-customer-tianyancha-design.md`（D1-D5 已决定，见其 18 节）；同时修订 `docs/superpowers/specs/2026-10-16-user-permission-backend-design.md` 的 7.4 与 14 节（已完成）。

## Global Constraints

- 错误码：客户 `7xxx`（7001 不存在、7002 信用代码重复、7003 字段校验、7004 负责人不可用）；天眼查 `8xxx`（8001 未配置、8002 无法解密、8003 上游不可用、8004 限流、8005 额度不足、8006 鉴权失败、8007 无结果、8008 格式不符）。`6xxx` 为监控预留，不得占用。
- 天眼查鉴权：请求头 `Authorization: <API Key>`，端点 `https://mcp.tianyancha.com/mcp`，Streamable HTTP JSON-RPC。
- 天眼查超时：连接 5 秒，读取 10 秒（与 `app/clients/feishu.py` 的 `DEFAULT_TIMEOUT` 一致）。
- 密钥：`CONFIG_ENCRYPTION_KEY` 必填，缺失或非法 Fernet 密钥时应用拒绝启动（D2）。
- 客户负责人转移：归属部门 `owner_department_id` 重新快照（D5）。
- 完整经营范围：取 `get_historical_registration` 的 `changeList` 中分类为"经营范围"、且"变更后原文"以截断前缀开头的最新一条；否则保留截断值并置 `business_scope_truncated=true`（D1）。
- 日志与响应中不得出现 token 明文或 Authorization 头。
- 注释与文档中文；标识符与表名英文；结构变更只走 Alembic。

## Review Focus

以下五类输入最可能让使用者出错，每一条都由所属任务的测试覆盖：

1. 天眼查返回空结果（`空结果：未发现该维度记录`）时，应得到"无候选"（空列表、total=0），而不是把空文本当成功解析。所属：Task 7（`test_empty_result_text_is_zero_candidates`）。
2. 候选表中信用代码为 `-`（境外主体）时，不能导入，也不能参与去重。所属：Task 7（解析为 None）与 Task 10（`import` 对 None 不做去重，且拒绝导入）。
3. 简称检索的翻页：第 2 页结果不得与第 1 页重复或丢失。所属：Task 10（`test_search_pages_do_not_overlap`，以 fake client 返回两页不同候选，断言 `page=2` 的 `items` 与 `page=1` 无交集，且 `total` 一致）。
4. 用户只能看到自己数据范围内的客户；不可见客户的信用代码重复，不得泄露其 ID。所属：Task 6（范围过滤）与 Task 10（`test_duplicate_hidden_from_viewer_omits_existing_id`）。
5. 天眼查返回的主体与请求的信用代码不一致时，不得写库。所属：Task 10（`test_import_refuses_when_upstream_subject_differs`）。

---

### Task 0: 前置条件（不写代码，只核对）

**Files:** 无。

- [ ] **Step 1: 确认监控分支已合并到 master，且迁移 head 唯一**

Run: `cd /home/ubuntu/tianxiadmin && git log --oneline master | head -3 && uv run alembic heads`
Expected: 仅一个 head，且为 `a0b141e79d98`（监控分支的迁移）。若监控尚未合并，本计划的迁移 `down_revision` 必须改为 `8db3ba95b766` 并在合并时重新排序，否则出现双 head。

- [ ] **Step 2: 确认测试库可用**

Run: `uv run pytest -q`
Expected: 全部通过（基线）。

---

### Task 1: 加密配置与 fail-fast 启动校验

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/main.py`（`_validate_critical_settings`）
- Modify: `.env.example`
- Modify: `tests/conftest.py`（`test_settings` 与 `os.environ.setdefault` 增加 `CONFIG_ENCRYPTION_KEY`）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.config_encryption_key: str`（必填）；`app.core.crypto.Cipher` 不在本任务创建。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 增加：

```python
def test_config_encryption_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://u:p@h/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "s")
    monkeypatch.delenv("CONFIG_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_config.py::test_config_encryption_key_is_required -v`
Expected: FAIL（字段尚不存在，未抛 ValidationError）。

- [ ] **Step 3: 实现**

在 `Settings` 中增加 `config_encryption_key: str`（无默认值）。在 `main.py` 的 `_validate_critical_settings` 中把 `"config_encryption_key"` 加入必填列表，并增加格式校验：尝试 `Fernet(settings.config_encryption_key)`，失败则抛 `RuntimeError("Invalid config_encryption_key")`。`.env.example` 增加占位 `CONFIG_ENCRYPTION_KEY=`（注释说明用 `Fernet.generate_key()` 生成）。`tests/conftest.py` 中 `os.environ.setdefault("CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())`，并在 `test_settings` fixture 中传入同一值。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/config.py app/main.py .env.example tests/conftest.py tests/test_config.py
git commit -m "feat: require config encryption key at startup"
```

---

### Task 2: 错误码与日志脱敏

**Files:**
- Modify: `app/core/errors.py`
- Modify: `app/core/logging.py`（`SENSITIVE_KEYS`、`_SENSITIVE_VALUE_PATTERN`）
- Test: `tests/test_response_and_errors.py`（扩充）、`tests/test_logging_redaction.py`（扩充）

**Interfaces:**
- Produces: `ErrorCode` 新增 `CUSTOMER_NOT_FOUND = 7001`、`CUSTOMER_CREDIT_CODE_EXISTS = 7002`、`CUSTOMER_INVALID_FIELD = 7003`、`CUSTOMER_OWNER_UNAVAILABLE = 7004`、`TIANYANCHA_NOT_CONFIGURED = 8001`、`TIANYANCHA_TOKEN_UNREADABLE = 8002`、`TIANYANCHA_UNAVAILABLE = 8003`、`TIANYANCHA_RATE_LIMITED = 8004`、`TIANYANCHA_QUOTA_EXCEEDED = 8005`、`TIANYANCHA_AUTH_FAILED = 8006`、`TIANYANCHA_NOT_FOUND = 8007`、`TIANYANCHA_MALFORMED_RESPONSE = 8008`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_logging_redaction.py` 增加：

```python
def test_api_key_is_masked() -> None:
    assert mask_sensitive_text("api_key=mcpk2_secret") == "api_key=***"
```

在 `tests/test_response_and_errors.py` 增加对 12 个新错误码数值的断言（逐个 `assert ErrorCode.X == NNNN`）。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_logging_redaction.py tests/test_response_and_errors.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

`errors.py` 按 Interfaces 增加枚举值。`logging.py`：`SENSITIVE_KEYS` 加入 `"api_key"`；`_SENSITIVE_VALUE_PATTERN` 的 key 分组加入 `api_key`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_logging_redaction.py tests/test_response_and_errors.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/errors.py app/core/logging.py tests/test_logging_redaction.py tests/test_response_and_errors.py
git commit -m "feat: add customer and tianyancha error codes and mask api_key"
```

---

### Task 2.5: BusinessError 支持 extra 并渲染到响应体

**Files:**
- Modify: `app/core/errors.py`（`BusinessError.__init__` 增加 `extra: dict[str, object] | None = None`，保存为 `self.extra`）
- Modify: `app/core/exception_handlers.py`（`business_error_handler` 把非空 `extra` 合并进 `content`）
- Test: `tests/test_response_and_errors.py`（扩充）

**Interfaces:**
- Produces: `BusinessError(code, message, http_status=400, extra=None)`；`exc.extra` 属性。
- Consumes: Task 2 的错误码（仅用于测试，非必需）。

- [ ] **Step 1: 写失败测试**

```python
async def test_business_error_extra_is_rendered(client) -> None:
    # 通过一个测试专用路由抛出 BusinessError(..., extra={"existing_customer_id": 9})
    r = await client.get("/__test__/extra")
    assert r.json()["existing_customer_id"] == 9

def test_business_error_default_extra_is_none() -> None:
    assert BusinessError(1000, "x").extra is None
```

（`/__test__/extra` 为测试内挂载的临时路由，仅在测试应用实例上注册。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_response_and_errors.py -k extra -v`
Expected: FAIL（`extra` 属性不存在）。

- [ ] **Step 3: 实现**

`BusinessError.__init__` 增加参数并赋值。`_error_response` 增加可选 `extra` 关键字，非空时 `content.update(extra)`；`business_error_handler` 传入 `exc.extra`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_response_and_errors.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/errors.py app/core/exception_handlers.py tests/test_response_and_errors.py
git commit -m "feat: allow business errors to carry extra response fields"
```

---

### Task 3: Fernet 加解密封装

**Files:**
- Create: `app/core/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: Task 2 的 `ErrorCode.TIANYANCHA_TOKEN_UNREADABLE`。
- Produces:
  - `def encrypt_secret(plain: str) -> str`
  - `def decrypt_secret(ciphertext: str) -> str`，失败抛 `BusinessError(ErrorCode.TIANYANCHA_TOKEN_UNREADABLE, ..., http_status=503)`。

- [ ] **Step 1: 写失败测试**

```python
def test_round_trip_returns_plaintext() -> None:
    token = "mcpk2_example_value"
    assert decrypt_secret(encrypt_secret(token)) == token

def test_ciphertext_does_not_contain_plaintext() -> None:
    assert "mcpk2_example" not in encrypt_secret("mcpk2_example_value")

def test_tampered_ciphertext_raises_unreadable() -> None:
    with pytest.raises(BusinessError) as exc:
        decrypt_secret(encrypt_secret("x")[:-4] + "AAAA")
    assert exc.value.code == ErrorCode.TIANYANCHA_TOKEN_UNREADABLE
    assert exc.value.http_status == 503
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_crypto.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现**

`Fernet(settings.config_encryption_key)`，模块级构造一次。`decrypt_secret` 捕获 `cryptography.fernet.InvalidToken`，抛出 `BusinessError`，消息为通用文本，不包含密文。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_crypto.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/crypto.py tests/test_crypto.py
git commit -m "feat: add fernet secret encryption helpers"
```

---

### Task 4: 集成配置模型、迁移与仓储

**Files:**
- Create: `app/models/integration_config.py`
- Modify: `app/models/__init__.py`
- Create: `app/repositories/integration_config.py`
- Create: `migrations/versions/<rev>_create_integration_config.py`（由 `alembic revision --autogenerate` 生成）
- Test: `tests/test_integration_config_repository.py`

**Interfaces:**
- Produces:
  - `IntegrationConfig` ORM：`provider: str`（唯一）、`secret_ciphertext: str`、`updated_by: int | None`，继承 `Base`。
  - `async def get_by_provider(session, provider: str) -> IntegrationConfig | None`
  - `async def upsert(session, provider: str, ciphertext: str, updated_by: int) -> IntegrationConfig`

迁移说明：每张表使用独立迁移文件（`integration_config` 在本任务，`customer` 在 Task 5）。不修改已生成的迁移，以免已应用的库与文件不一致。

- [ ] **Step 1: 写失败测试**

```python
async def test_upsert_creates_then_updates_single_row(session: AsyncSession) -> None:
    first = await upsert(session, "tianyancha", "cipher-1", updated_by=1)
    second = await upsert(session, "tianyancha", "cipher-2", updated_by=2)
    assert first.id == second.id
    fetched = await get_by_provider(session, "tianyancha")
    assert fetched is not None
    assert fetched.secret_ciphertext == "cipher-2"
    assert fetched.updated_by == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_integration_config_repository.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现模型与仓储**

模型字段 `provider` 为 `String(64)` 且 `unique=True`；`secret_ciphertext` 为 `Text`；`updated_by` 为 `BigInteger`，可空，不加外键（审计语义，与日志表一致）。`upsert` 先查后改，同一事务内 `flush`。

- [ ] **Step 4: 生成并应用迁移**

Run: `uv run alembic revision --autogenerate -m "create integration config"`，然后检查生成文件的 `down_revision` 为 `a0b141e79d98`（见 Task 0）。
Run: `uv run alembic upgrade head`
Expected: 无错误；`uv run alembic downgrade -1 && uv run alembic upgrade head` 也无错误。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_integration_config_repository.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/models/integration_config.py app/models/__init__.py app/repositories/integration_config.py migrations/versions/*create_integration_config.py tests/test_integration_config_repository.py
git commit -m "feat: add integration config model and migration"
```

---

### Task 5: 客户模型与仓储

**Files:**
- Create: `app/models/customer.py`
- Modify: `app/models/__init__.py`
- Create: `app/repositories/customer.py`
- Create: `migrations/versions/<rev>_create_customer.py`（`alembic revision --autogenerate -m "create customer"`，`down_revision` 指向 Task 4 生成的迁移 id）
- Test: `tests/test_customer_repository.py`

**Interfaces:**
- Produces:
  - `CustomerSource(StrEnum)`：`TIANYANCHA = "TIANYANCHA"`、`MANUAL = "MANUAL"`
  - `Customer` ORM，字段见设计 7.1：`name`, `short_name`, `credit_code`（唯一，可空）, `legal_person`, `registered_capital`, `established_date: date | None`, `business_scope`, `business_scope_truncated: bool`, `reg_status`, `source: CustomerSource`, `tyc_imported_at: datetime | None`, `remark`, `owner_user_id: int`（外键 `user.id`，RESTRICT）, `owner_department_id: int | None`（外键 `department.id`，RESTRICT）。
  - `async def get_by_id(session, customer_id) -> Customer | None`
  - `async def get_by_credit_code(session, credit_code) -> Customer | None`
  - `async def add(session, customer) -> Customer`
  - `async def delete(session, customer) -> None`
  - `async def list_customers(session, *, page, page_size, scope_filter, keyword, source) -> tuple[list[Customer], int]`
  - `async def credit_codes_existing(session, codes: list[str]) -> dict[str, Customer]`（用于候选标注，返回 `credit_code → Customer`，调用方再按可见性裁剪）

- [ ] **Step 1: 写失败测试**

```python
async def test_credit_code_is_unique(session: AsyncSession, make_user) -> None:
    owner = await make_user()
    await add(session, make_customer(owner, credit_code="911100007693890511"))
    with pytest.raises(IntegrityError):
        await add(session, make_customer(owner, credit_code="911100007693890511"))

async def test_null_credit_codes_do_not_conflict(session, make_user) -> None:
    owner = await make_user()
    await add(session, make_customer(owner, credit_code=None))
    await add(session, make_customer(owner, credit_code=None))
```

（`make_user`、`make_customer` 为测试内的辅助工厂，写在 `tests/test_customer_repository.py` 顶部；需要 `department` 行的场景复用 `tests/test_user_crud.py` 的种子方式。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_customer_repository.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现模型与仓储，并在迁移中追加表**

`credit_code` 为 `String(18)`，`unique=True`，可空（MySQL 允许多个 NULL）。`owner_user_id` 与 `owner_department_id` 建索引。`list_customers` 结构与 `user_repository.list_users` 一致（`scope_filter` 作为首个条件）。

- [ ] **Step 4: 应用迁移**

Run: `uv run alembic upgrade head`（测试库同样，`DATABASE_URL=... uv run alembic upgrade head`）
Expected: 无错误。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_customer_repository.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/models/customer.py app/models/__init__.py app/repositories/customer.py migrations/versions/*create_customer.py tests/test_customer_repository.py
git commit -m "feat: add customer model and repository"
```

---

### Task 6: 统一数据范围过滤（D3）

**Files:**
- Modify: `app/services/data_scope.py`
- Test: `tests/test_data_scope.py`（扩充，既有用例必须保持通过）

**Interfaces:**
- Produces（替代 `build_user_scope_filter` 的内部实现，外部签名保持兼容）：
  - `def build_scope_filter(ctx: AuthContext, *, department_column, owner_column) -> ColumnElement[bool]`
  - `build_user_scope_filter(ctx)` 变为调用 `build_scope_filter(ctx, department_column=User.department_id, owner_column=User.id)`
  - `build_customer_scope_filter(ctx)` 调用 `build_scope_filter(ctx, department_column=Customer.owner_department_id, owner_column=Customer.owner_user_id)`
  - `apply_customer_scope(stmt, ctx) -> Select`

规则（设计 10.2）：`ALL` 与超管不加条件；`SELF` 仅 `owner_column == ctx.user_id`；DEPT 类与 CUSTOM 要求"部门列"落在范围内，且当前用户部门为业务部门（`is_system=False`）。`department_column` 为 NULL 时 DEPT 类不可见（SQL 中 `IN` 与 NULL 自然不匹配，但需显式用例覆盖）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_data_scope.py` 增加：

```python
def test_customer_scope_self_matches_owner_user_id() -> None:
    ctx = _ctx(data_scope=DataScope.SELF, user_id=7)
    sql = str(build_customer_scope_filter(ctx).compile(compile_kwargs={"literal_binds": True}))
    assert "customer.owner_user_id = 7" in sql

def test_customer_dept_scope_uses_owner_department_column() -> None:
    ctx = _ctx(data_scope=DataScope.DEPT, department_id=3)
    sql = str(build_customer_scope_filter(ctx).compile(compile_kwargs={"literal_binds": True}))
    assert "customer.owner_department_id" in sql

def test_user_scope_filter_unchanged_after_refactor() -> None:
    # 期望字符串在 Task 6 Step 3 开始前，用当前 master 的 build_user_scope_filter 生成并固定到测试中
    ctx = _ctx(data_scope=DataScope.DEPT, department_id=3)
    sql = str(build_user_scope_filter(ctx).compile(compile_kwargs={"literal_binds": True}))
    assert sql == EXPECTED_USER_DEPT_SQL  # 常量在测试文件顶部，由实施者按上述方法固定
```

（`_ctx` 为既有测试中的上下文构造辅助函数。`EXPECTED_USER_DEPT_SQL` 必须在重构前从 master 取得，重构后逐字比对，任何差异都视为回归。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_data_scope.py -v`
Expected: 新增用例 FAIL（`build_customer_scope_filter` 不存在）。

- [ ] **Step 3: 实现**

把 `build_user_scope_filter` 中的分支逻辑迁入 `build_scope_filter`，参数化两列。`test_user_scope_filter_unchanged_after_refactor` 中的期望字符串在重构前用 `git stash`/当前代码生成并固定。

- [ ] **Step 4: 运行全部数据范围与用户测试**

Run: `uv run pytest tests/test_data_scope.py tests/test_user_crud.py tests/test_e2e_permission_flow.py -v`
Expected: PASS（用户行为不变）。

- [ ] **Step 5: 提交**

```bash
git add app/services/data_scope.py tests/test_data_scope.py
git commit -m "refactor: share department scope filter between users and customers"
```

---

### Task 7: 天眼查 MCP 客户端（纯解析 + 传输）

**Files:**
- Create: `app/clients/tianyancha.py`
- Create: `tests/fixtures/tianyancha/search_companies.md`（从实测样本脱敏，保留表头与 2 行）
- Create: `tests/fixtures/tianyancha/registration.md`（工商登记，保留"经营范围"截断行）
- Create: `tests/fixtures/tianyancha/historical_registration_excerpt.md`（历史工商，仅保留 `changeList` 中 2 条经营范围变更）
- Test: `tests/test_tianyancha_client.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Candidate: credit_code: str | None; name: str; legal_person: str | None; reg_status: str | None; company_id: str`
  - `@dataclass(frozen=True) class RegistrationInfo: name: str; credit_code: str; legal_person: str | None; registered_capital: str | None; established_date: str | None; business_scope: str | None; reg_status: str | None`
  - `class TianyanchaClient`：构造参数 `(api_key: str, transport: httpx.AsyncBaseTransport | None = None)`；方法：
    - `async def test_connection(self) -> None`
    - `async def search_candidates(self, keyword: str, *, page: int, page_size: int) -> tuple[list[Candidate], int]`
    - `async def fetch_registration(self, credit_code: str) -> RegistrationInfo`
    - `async def fetch_historical_registration_text(self, credit_code: str) -> str`（返回原始 Markdown，供 Task 8 的经营范围算法使用）
  - `def parse_search_table(markdown: str) -> tuple[list[Candidate], int]`（纯函数）
  - `def parse_registration_table(markdown: str) -> RegistrationInfo`（纯函数）
  - `def parse_change_scope_records(markdown: str) -> list[tuple[str, str]]`（返回 `(变更时间, 变更后原文)`，纯函数，供 Task 8 使用）
  - `def map_upstream_error(text_or_status) -> BusinessError`（见下方规则）

传输：`POST https://mcp.tianyancha.com/mcp`，头 `Authorization`、`Accept: application/json, text/event-stream`、`Content-Type: application/json`，首次 `initialize` 后取 `Mcp-Session-Id` 头回传。响应可能是 JSON 或 SSE：实测为 `application/json`，实现时按 `Content-Type` 分支处理 SSE（取 `data:` 行）。`tools/call` 的文本位于 `result.content[0].text`。

- [ ] **Step 1: 写失败测试（纯解析，使用 fixture）**

```python
def test_parse_search_table_extracts_candidates_and_total() -> None:
    md = (FIXTURES / "search_companies.md").read_text(encoding="utf-8")
    candidates, total = parse_search_table(md)
    assert total == 111
    assert candidates[0].name == "乐视网信息技术（北京）股份有限公司"
    assert candidates[0].credit_code == "911100007693890511"
    assert candidates[0].company_id == "14427175"

def test_parse_search_table_keeps_dash_credit_code_as_none() -> None:
    md = "| 8 | 樂視網信息技術（香港）有限公司 | - | 仍注册 | YANFENG LIU | - | 2011-05-24 00:00:00.0 | 2 | 2011-05-24 00:00:00.0 | 3086162174 | 公司名称匹配 |"
    candidates, _ = parse_search_table(md)
    assert candidates[0].credit_code is None

def test_parse_registration_keeps_truncation_marker() -> None:
    info = parse_registration_table((FIXTURES / "registration.md").read_text(encoding="utf-8"))
    assert info.business_scope.endswith("...")

def test_empty_result_text_is_zero_candidates() -> None:
    candidates, total = parse_search_table("# 企业候选列表\n\n> 空结果：未发现该维度记录\n")
    assert candidates == [] and total == 0

def test_map_upstream_error_quota() -> None:
    err = map_upstream_error({"code": 300007})
    assert err.code == ErrorCode.TIANYANCHA_QUOTA_EXCEEDED and err.http_status == 402
```

（上游错误的 JSON-RPC 错误体格式与 `300007` 字段位置未实测，`map_upstream_error` 的输入形态在 Task 7 Step 3 的真实调用中确认后再固定，见下方说明。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tianyancha_client.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现解析与传输**

解析按列名映射：从表头行取列索引，不依赖固定列号。`total` 取自正文"总数 N 条"。`-` 视为 None。传输使用 `httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), transport=transport)`。

**真实错误形态（实现前必做）：** 用一个超过额度或无效的 token 调一次 `tools/call`，记录 HTTP 状态与响应体结构（只记录结构，不记录 token），把结果写入 Task 7 的测试 `test_map_upstream_error_*` 的输入。已知：无效 token → HTTP 401，体为 `{"error":"invalid_token","error_description":"invalid MCP API key"}`。额度不足的形态未知，若无法在不耗尽额度的前提下复现，则按 HTTP 402 与 `quota_exceeded` 字符串匹配，并在测试中用文档化的 `300007`/`quota_exceeded` 两种形态覆盖。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tianyancha_client.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/clients/tianyancha.py tests/fixtures/tianyancha tests/test_tianyancha_client.py
git commit -m "feat: add tianyancha mcp client with markdown table parsers"
```

---

### Task 8: 完整经营范围算法（D1）

**Files:**
- Create: `app/services/customer_scope_text.py`
- Test: `tests/test_customer_scope_text.py`

**Interfaces:**
- Consumes: `parse_change_scope_records(markdown) -> list[tuple[str, str]]`（Task 7，返回 `(变更时间, 变更后原文)`，其中变更时间为 `YYYY-MM-DD`）
- Produces:
  - `def resolve_full_business_scope(displayed: str | None, change_records: list[tuple[str, str]]) -> tuple[str | None, bool]`，返回 `(经营范围, 是否截断)`。

算法（设计 6.4）：`displayed` 为 None 时返回 `(None, False)`。去掉结尾的 `...`（或 `…`）得到 `prefix`；在 `change_records` 中取"变更后原文"以 `prefix` 开头的记录，按变更时间降序取第一条；无则返回 `(displayed, True)`。

- [ ] **Step 1: 写失败测试**

```python
def test_returns_latest_full_text_matching_prefix() -> None:
    displayed = "一般项目：软件开发；技术服务、技术开发、技术咨询、技术交流、技术转让、技术推广；计算机系统服务；计算机软硬件及辅助设备批发；计算机软硬件及辅助设备零售；服装服饰批发；服装服饰零售；鞋帽批发；鞋帽零售；皮革制品销售；针纺织品销售；日用品销售；玩具销售；文具用品批发；文具用品零售；体育用品及器材批发；体育用品及器材零售；家..."
    records = [("2022-03-11", displayed[:-3] + "用品销售。（完整）"), ("2018-11-05", "其他经营范围")]
    full, truncated = resolve_full_business_scope(displayed, records)
    assert full.startswith(displayed[:-3]) and full.endswith("（完整）")
    assert truncated is False

def test_no_matching_record_keeps_truncated_and_flags() -> None:
    full, truncated = resolve_full_business_scope("一般项目：A...", [("2020-01-01", "完全无关")])
    assert full == "一般项目：A..." and truncated is True

def test_older_record_does_not_beat_newer_when_ordering_is_shuffled() -> None:
    displayed = "一般项目：A..."
    records = [("2010-01-01", "一般项目：A旧版本"), ("2022-01-01", "一般项目：A新版本")]
    full, _ = resolve_full_business_scope(displayed, records)
    assert full == "一般项目：A新版本"

def test_none_displayed_returns_none_not_truncated() -> None:
    assert resolve_full_business_scope(None, []) == (None, False)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_customer_scope_text.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

按变更时间字符串（`YYYY-MM-DD`，可直接字典序比较）降序排序后取第一条匹配项。`changeList` 的记录缺少时间时视为最旧（空串排在最后）。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_customer_scope_text.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/customer_scope_text.py tests/test_customer_scope_text.py
git commit -m "feat: resolve full business scope from change history"
```

---

### Task 9: 集成配置服务与天眼查 token 管理

**Files:**
- Create: `app/services/integration.py`
- Test: `tests/test_integration_service.py`

**Interfaces:**
- Consumes: Task 3 `encrypt_secret`/`decrypt_secret`；Task 4 `integration_config` 仓储；Task 7 `TianyanchaClient`。
- Produces:
  - `async def get_tianyancha_status(session) -> dict` → `{configured: bool, updated_at: datetime | None, updated_by: int | None}`
  - `async def save_tianyancha_token(session, api_key: str, *, context: AuthContext) -> None`（加密后 upsert；写审计装饰器见 Task 11）
  - `async def get_tianyancha_client(session) -> TianyanchaClient`：未配置抛 `8001`；解密失败抛 `8002`（由 `decrypt_secret` 抛出）。
  - `async def test_tianyancha_connection(session, *, context) -> None`

- [ ] **Step 1: 写失败测试**

```python
async def test_status_reports_not_configured_then_configured(session, context) -> None:
    assert (await get_tianyancha_status(session))["configured"] is False
    await save_tianyancha_token(session, "mcpk2_x", context=context)
    status = await get_tianyancha_status(session)
    assert status["configured"] is True
    assert "mcpk2_x" not in str(status)

async def test_client_raises_not_configured(session) -> None:
    with pytest.raises(BusinessError) as exc:
        await get_tianyancha_client(session)
    assert exc.value.code == ErrorCode.TIANYANCHA_NOT_CONFIGURED
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_integration_service.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

`save_tianyancha_token` 对 `api_key` 做非空与去首尾空白处理；空值抛 `BusinessError(ErrorCode.INVALID_PARAMETER, ..., 422)`。状态接口绝不返回密文或明文。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_integration_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/integration.py tests/test_integration_service.py
git commit -m "feat: add tianyancha token management service"
```

---

### Task 10: 客户服务（检索、回填、CRUD、负责人转移）

**Files:**
- Create: `app/services/customer.py`
- Create: `app/schemas/customer.py`
- Test: `tests/test_customer_service.py`

**Interfaces:**
- Consumes: Task 5 仓储；Task 6 `apply_customer_scope`、`build_customer_scope_filter`；Task 7 `TianyanchaClient`；Task 8 `resolve_full_business_scope`；Task 9 `get_tianyancha_client`。
- Produces:
  - `async def search_candidates(session, keyword, *, page, page_size, context) -> dict`（返回 `{items, total, page, page_size}`，每项含 `existing_customer_id | None`，且仅在可见时给出；`existed_hidden: bool` 标注不可见的已存在）
  - `async def import_from_tianyancha(session, credit_code, short_name, *, context) -> Customer`
  - `async def create_customer(session, payload, *, context) -> Customer`
  - `async def get_customer(session, customer_id, *, context) -> Customer`
  - `async def update_customer(session, customer_id, payload, *, context) -> Customer`
  - `async def delete_customer(session, customer_id, *, context) -> None`
  - `async def transfer_owner(session, customer_id, owner_user_id, *, context) -> Customer`
  - `async def list_customers(session, *, context, page, page_size, keyword, source) -> tuple[list[Customer], int]`

规则要点（与设计一致）：
- `import_from_tianyancha`：信用代码格式校验（18 位，`^[0-9A-Z]{18}$`）→ 查重（查到即抛 `7002`，可见才附 `existing_customer_id`，见 `_raise_duplicate`）→ `fetch_registration(credit_code)` → 校验返回的 `credit_code` 与请求一致否则抛 `8008` → `fetch_historical_registration_text` + `parse_change_scope_records` + `resolve_full_business_scope` → 写入（`source=TIANYANCHA`，`owner_user_id=context.user_id`，`owner_department_id=context.department_id`）→ 唯一约束冲突（`IntegrityError`）转 `7002`。
- `transfer_owner`：先按可见性取客户（不可见 `7001`）；目标用户必须存在、`status=active`，且满足 `build_user_scope_filter(context)`（可见）否则 `7004`；更新 `owner_user_id` 并把 `owner_department_id` 设为目标用户当前 `department_id`。
- `search_candidates`：调用 `client.search_candidates`；对结果中的 `credit_code` 调 `credit_codes_existing` 标注；`credit_code` 为 None 的候选不做去重标注。

- [ ] **Step 1: 写失败测试（以 fake client 注入，不调用真实天眼查）**

```python
async def test_import_rejects_existing_credit_code_without_calling_upstream(session, context, fake_client) -> None:
    await create_customer_row(session, credit_code="911100007693890511", owner=context)
    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, "911100007693890511", None, context=context)
    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert fake_client.calls == []

async def test_import_rejects_malformed_credit_code(session, context, fake_client) -> None:
    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, "bad", None, context=context)
    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD
    assert fake_client.calls == []

async def test_import_refuses_when_upstream_subject_differs(session, context, fake_client) -> None:
    fake_client.registration_credit_code = "91110000000000000X"
    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, "911100007693890511", None, context=context)
    assert exc.value.code == ErrorCode.TIANYANCHA_MALFORMED_RESPONSE
    assert await count_customers(session) == 0

async def test_duplicate_hidden_from_viewer_omits_existing_id(session, other_context, owner_customer) -> None:
    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, owner_customer.credit_code, None, context=other_context)
    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.extra is None  # 不可见时不附带 existing_customer_id

async def test_search_pages_do_not_overlap(session, context, fake_client) -> None:
    fake_client.pages = {1: [c("111"), c("222")], 2: [c("333"), c("444")]}
    first = await search_candidates(session, "乐视网", page=1, page_size=2, context=context)
    second = await search_candidates(session, "乐视网", page=2, page_size=2, context=context)
    ids_first = {i["company_id"] for i in first["items"]}
    ids_second = {i["company_id"] for i in second["items"]}
    assert ids_first.isdisjoint(ids_second)
    assert first["total"] == second["total"]

async def test_transfer_to_unavailable_user_is_rejected(session, context, disabled_user, customer) -> None:
    with pytest.raises(BusinessError) as exc:
        await transfer_owner(session, customer.id, disabled_user.id, context=context)
    assert exc.value.code == ErrorCode.CUSTOMER_OWNER_UNAVAILABLE

async def test_transfer_snapshots_new_owner_department(session, context, target_user, customer) -> None:
    await transfer_owner(session, customer.id, target_user.id, context=context)
    refreshed = await get_customer_row(session, customer.id)
    assert refreshed.owner_user_id == target_user.id
    assert refreshed.owner_department_id == target_user.department_id
```

（`fake_client` 为测试内替身，实现 Task 7 的三个方法；`context`、`other_context` 等为既有测试风格的上下文构造。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_customer_service.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

重复信用代码时，服务层构造 `BusinessError(ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS, "...", http_status=409, extra={"existing_customer_id": id})`，且仅在调用者可见时传入 `extra`，否则 `extra=None`。`extra` 的基础设施已在 Task 2.5 中完成，本任务不再修改 errors.py 与 exception_handlers.py。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_customer_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/customer.py app/schemas/customer.py tests/test_customer_service.py
git commit -m "feat: add customer service with tianyancha import and owner transfer"
```

---

### Task 11: 写操作审计接入

**Files:**
- Modify: `app/services/customer.py`、`app/services/integration.py`（为写用例加装饰器）
- Test: `tests/test_customer_audit.py`

**Interfaces:**
- Consumes: 监控分支的 service 审计装饰器（`app/services/audit.py`，监控分支 Task 6 产出）；`AuthContext.username/nickname`（监控分支 Task 2 产出）。
- Produces: 审计记录的 `action` 与 `resource_type`：`customer:create` / `customer:update` / `customer:delete` / `customer:transfer` / `integration:update`，`resource_type` 为 `customer` 与 `integration`。

**前置：** 本任务依赖监控分支已合并。若未合并，本任务整体推迟，其余任务不受影响。

- [ ] **Step 1: 写失败测试**

```python
async def test_transfer_writes_audit_row(session, context, target_user, customer) -> None:
    await transfer_owner(session, customer.id, target_user.id, context=context)
    row = await latest_audit(session)
    assert row.action == "customer:transfer"
    assert row.resource_type == "customer"
    assert row.resource_id == customer.id

async def test_save_token_audit_stores_no_secret(session, context) -> None:
    await save_tianyancha_token(session, "mcpk2_secret_value", context=context)
    row = await latest_audit(session)
    assert row.action == "integration:update"
    assert "mcpk2_secret_value" not in str(row.__dict__)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_customer_audit.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

用监控分支提供的装饰器标注 `create_customer`、`update_customer`、`delete_customer`、`transfer_owner`、`save_tianyancha_token`，`import_from_tianyancha` 记为 `customer:create`。装饰器签名以监控分支 `app/services/audit.py` 的实际定义为准，实现前读取它。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_customer_audit.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/customer.py app/services/integration.py tests/test_customer_audit.py
git commit -m "feat: audit customer and integration write use cases"
```

---

### Task 12: 接口层与路由注册

**Files:**
- Create: `app/api/customers.py`
- Create: `app/api/integrations.py`
- Modify: `app/api/router.py`
- Modify: `app/main.py`（`OPENAPI_TAGS` 增加"客户"与"集成"）
- Test: `tests/test_customer_api.py`

**Interfaces:**
- Consumes: Task 10 服务函数；`require_permission`；`get_current_context`。
- Produces（路由与权限，设计第 10.1 与 11 节）：
  - `GET /customers`（`customer:read`）、`POST /customers`（`customer:create`）
  - `GET /customers/search`（`customer:create`）、`POST /customers/import`（`customer:create`）
  - `GET /customers/{id}`（`customer:read`）、`PATCH /customers/{id}`（`customer:update`）、`DELETE /customers/{id}`（`customer:delete`）
  - `PATCH /customers/{id}/owner`（`customer:update`）
  - `GET /integrations/tianyancha`（`integration:read`）、`PUT /integrations/tianyancha`（`integration:update`）、`POST /integrations/tianyancha/test`（`integration:update`）
- 响应统一 `success(...)`；分页 `Page[T]`。

- [ ] **Step 1: 写失败测试**

```python
async def test_search_requires_customer_create(client_without_permission) -> None:
    r = await client_without_permission.get("/api/v1/customers/search", params={"keyword": "乐视网"})
    assert r.status_code == 403

async def test_token_response_never_contains_secret(client_super_admin) -> None:
    await client_super_admin.put("/api/v1/integrations/tianyancha", json={"api_key": "mcpk2_secret"})
    r = await client_super_admin.get("/api/v1/integrations/tianyancha")
    assert r.status_code == 200
    assert "mcpk2_secret" not in r.text
    assert r.json()["data"]["configured"] is True

async def test_import_quota_error_maps_to_402(client_sales, fake_tyc_quota_exceeded) -> None:
    # fake_tyc_quota_exceeded 使 TianyanchaClient 抛出 8005（上游 300007）
    r = await client_sales.post("/api/v1/customers/import", json={"credit_code": "911100007693890511"})
    assert r.status_code == 402
    assert r.json()["code"] == 8005
```

（`client_super_admin`、`client_without_permission` 复用 `tests/test_e2e_permission_flow.py` 中的测试客户端构造。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_customer_api.py -v`
Expected: FAIL（路由未注册）。

- [ ] **Step 3: 实现**

接口只做参数解析与响应组装；`PUT /integrations/tianyancha` 的请求体模型 `IntegrationTokenUpdate` 字段 `api_key: str`，`Field(min_length=1, max_length=256)`，并确保该模型的 `repr` 不输出明文（`Field(repr=False)`）。`GET /customers/search` 的 `page_size` 上限 50（设计 11 节），与全局 `MAX_PAGE_SIZE=100` 不同，需在路由参数中显式限定为 `le=50`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_customer_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/api/customers.py app/api/integrations.py app/api/router.py app/main.py app/schemas tests/test_customer_api.py
git commit -m "feat: expose customer and tianyancha integration endpoints"
```

---

### Task 12.5: 天眼查按用户限流（补充记录）

设计第 13 节要求把限流阈值记录在计划中。接口层已按用户实施固定窗口限流：

- 检索 `GET /customers/search`：每用户 20 次 / 60 秒。
- 回填 `POST /customers/import`：每用户 10 次 / 3600 秒。
- 超出阈值返回错误码 `RATE_LIMITED`（1005），HTTP 429。
- 实现位于 `app/core/rate_limit.py`（常量 `SEARCH_MAX_REQUESTS` / `SEARCH_WINDOW_SECONDS`、`IMPORT_MAX_REQUESTS` / `IMPORT_WINDOW_SECONDS`），由 `app/api/customers.py` 调用。

---

### Task 13: 菜单与权限码种子

**Files:**
- Modify: `scripts/init_data.py`（`_MENU_SEEDS` 追加）
- Modify: `tests/test_seed_data.py`（`EXPECTED_PERMISSION_CODES` 追加 6 个新权限码）

**Interfaces:**
- Consumes: 既有 `_MenuSeed` 结构与 `_get_or_create_menus`。
- Produces 菜单：`customer`（目录 `/customers`，父级顶层，sort 30）；`customer-list`（菜单 `/customers/list`，`customer:read`）；`customer-create`（按钮，`customer:create`）；`customer-update`（按钮，`customer:update`）；`customer-delete`（按钮，`customer:delete`）；`integration-tianyancha`（菜单 `/system/integrations/tianyancha`，`integration:read`，父级 `system`）；`integration-tianyancha-update`（按钮，`integration:update`，父级 `integration-tianyancha`）。

`default` 角色不挂载任何新菜单（设计 17 节）。`super_admin` 动态获得。

注意：监控分支也会改 `_MENU_SEEDS` 与 `EXPECTED_PERMISSION_CODES`（设计 15 节第 3 条）。合并时以"两边的并集"为准，`EXPECTED_PERMISSION_CODES` 必须与实际种子保持精确相等。

- [ ] **Step 1: 写失败测试**

在 `tests/test_seed_data.py` 中，把 `EXPECTED_PERMISSION_CODES` 扩充：

```python
EXPECTED_PERMISSION_CODES |= {
    "customer:read",
    "customer:create",
    "customer:update",
    "customer:delete",
    "integration:read",
    "integration:update",
}
```

并增加：

```python
async def test_integration_update_button_is_under_integration_menu(session) -> None:
    await init_data(session)
    button = await get_menu_by_permission(session, "integration:update")
    parent = await get_menu_by_id(session, button.parent_id)
    assert parent.permission_code == "integration:read"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_seed_data.py -v`
Expected: FAIL（种子未包含新权限码）。

- [ ] **Step 3: 实现**

在 `_MENU_SEEDS` 中按 Interfaces 追加，`key` 与 `parent_key` 按上表。不手工改表，只通过 `init_data` 写入。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_seed_data.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/init_data.py tests/test_seed_data.py
git commit -m "feat: seed customer and tianyancha menus and permissions"
```

---

### Task 14: 端到端与回归验证

**Files:**
- Test: `tests/test_customer_e2e.py`

**Interfaces:** 无新接口。

- [ ] **Step 1: 写端到端测试**

覆盖设计第 14 节的关键路径（以 fake 天眼查客户端注入，不消耗真实额度）：

```python
async def test_search_then_import_then_customer_visible_in_list(client_sales, fake_tyc) -> None:
    search = await client_sales.get("/api/v1/customers/search", params={"keyword": "乐视网"})
    assert search.status_code == 200
    credit_code = search.json()["data"]["items"][0]["credit_code"]

    imported = await client_sales.post("/api/v1/customers/import", json={"credit_code": credit_code})
    assert imported.status_code == 200
    data = imported.json()["data"]
    assert data["source"] == "TIANYANCHA"
    assert data["business_scope_truncated"] is False

    listed = await client_sales.get("/api/v1/customers", params={"keyword": credit_code})
    assert listed.json()["data"]["total"] == 1

async def test_department_scoped_user_cannot_see_other_department_customer(client_dept_a, customer_in_dept_b) -> None:
    r = await client_dept_a.get(f"/api/v1/customers/{customer_in_dept_b.id}")
    assert r.status_code == 404
    assert r.json()["code"] == 7001

async def test_transfer_then_original_owner_loses_visibility_under_self_scope(client_self, customer_owned_by_self, other_user) -> None:
    before = await client_self.get(f"/api/v1/customers/{customer_owned_by_self.id}")
    assert before.status_code == 200
    await client_self.patch(f"/api/v1/customers/{customer_owned_by_self.id}/owner", json={"owner_user_id": other_user.id})
    after = await client_self.get(f"/api/v1/customers/{customer_owned_by_self.id}")
    assert after.status_code == 404
```

- [ ] **Step 2: 运行全量测试与静态检查**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: 全部通过，无新增告警。

- [ ] **Step 3: 手工验证（超管本机，真实 token，不进入 CI）**

按设计第 14 节执行一次真实检索与一次真实回填，确认 6.2 的实测结论仍成立。这一步会消耗 2 到 3 次额度。

- [ ] **Step 4: 提交**

```bash
git add tests/test_customer_e2e.py
git commit -m "test: add customer end-to-end coverage"
```

---

## 分支合并顺序

1. 先合并 `system-monitoring-logging`（设计 15 节第 7 条的建议）。
2. 客户分支基于合并后的 master 变基（rebase）。
3. Task 0 核对迁移 head 唯一后再开始 Task 4。
4. Task 11 依赖监控分支的审计装饰器，若监控未合并则跳过，并在 PR 描述中说明。
</content>
