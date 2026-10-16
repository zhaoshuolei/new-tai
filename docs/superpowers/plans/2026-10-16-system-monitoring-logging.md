# 系统监控与日志 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 tianxiadmin 后端补齐操作审计日志、登录日志、系统监控（实时快照 + 定时采样）与异常监控（落库 + 后台查看/标记），四块共用独立事务写入与统一查询接口。

**Architecture:** 新增 4 张表（`audit_log`、`login_log`、`system_metric`、`exception_log`）。日志写入全部走独立 `session_factory()` 会话并带 2 秒有界超时，业务事务回滚不影响已落库记录。操作审计由 service 层装饰器（成功 + 授权类拒绝）与全局异常处理器（依赖层 403）双钩子产生，用异常实例上的标记去重。系统监控新增 `psutil` 依赖与一个随 `lifespan` 启停的 asyncio 采样任务，复用 `/health/ready` 的探活逻辑。异常监控挂在全局异常处理器的 5xx 路径上，支持按指纹分组汇总与单条 ack。

**Tech Stack:** Python 3.14、FastAPI、SQLAlchemy 2.0 async + asyncmy、Alembic、MySQL 8、Redis、psutil（新增）、pytest + pytest-asyncio + httpx。

**Spec:** [docs/superpowers/specs/2026-10-16-system-monitoring-logging-design.md](../specs/2026-10-16-system-monitoring-logging-design.md)

## Global Constraints

- 依赖方向单向 `api → services → repositories → models`；禁止路由内直接写 SQL；repository 不自行 `commit`（日志写入器除外，它自成事务边界并在 service 层）。
- 接口前缀 `/api/v1`；成功体 `{"code": 0, "message": "ok", "data": ...}`；分页 `{"items", "total", "page", "page_size"}`，`page_size` 上限 100。
- 时间 UTC 存 `DATETIME(6)`，API 返回 ISO8601 带时区；表名列名 snake_case，注释文档中文。
- 业务错误码分段：`6xxx` 为日志/监控段预留（本期无新增业务错误码）。
- 所有日志写入：独立会话 + `asyncio.wait_for` 超时 2 秒，失败只记文件日志，绝不抛回主请求。
- 写入异常日志前必须过 `mask_sensitive_text` 脱敏。
- Redis 键一律带 TTL；本设计除沿用既有键外不新增 Redis 键。
- 测试数据库为 `tianxiadmin_test`（conftest 默认 `mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test`）；每个迁移任务后须对测试库执行 `alembic upgrade head`。
- 质量门：`uv run ruff check app tests scripts`、`uv run mypy app`（strict）、相关 `pytest` 全绿。
- 权限码命名 `资源:动作`；新权限码：`audit:read`、`login_log:read`、`monitor:read`、`exception:read`、`exception:update`。

## Review Focus

1. **业务回滚吞掉拒绝审计** — 停用最后一个超管被拒时业务事务回滚，若审计共用该事务则"越权尝试"无影无踪。期望：回滚后 `audit_log` 中仍有 `result=denied` 行。→ Task 4 Step 1 的 `test_denied_audit_survives_rollback`。
2. **同一次拒绝记两条** — service 装饰器记完后全局处理器再记一次（或反之漏记依赖层 403）。期望：service 层拒绝恰好 1 条、依赖层 403 恰好 1 条。→ Task 5 Step 1 的 `test_service_denial_recorded_once` 与 `test_dependency_denial_recorded`。
3. **保留策略误删或关不掉** — `RETENTION_DAYS=0` 仍执行删除会清空审计；按错时间列清理会误删新数据。期望：`0` 不产生 DELETE；清理只删超过保留天数的行。→ Task 13 Step 1 的 `test_retention_zero_disables_cleanup`、`test_retention_deletes_only_expired`。
4. **异常消息泄露敏感值** — 异常文本里带 token/密码直接落库。期望：`exception_log.message` 中敏感值已替换为 `***`。→ Task 9 Step 1 的 `test_exception_message_masked`。
5. **采样任务拖挂启停** — lifespan 启动时采样阻塞、关闭时任务不退出导致进程挂住。期望：启动立即就绪、关闭可取消。→ Task 10 Step 1 的 `test_sampler_task_cancelled_on_shutdown`。

---

### Task 1: 四张日志表模型 + 配置项 + 迁移

**Files:**
- Create: `app/models/audit_log.py`, `app/models/login_log.py`, `app/models/system_metric.py`, `app/models/exception_log.py`
- Modify: `app/models/__init__.py`, `app/core/config.py`, `.env.example`
- Test: `tests/test_log_models.py`

**Interfaces:**
- Produces:
  - `AuditLog`（字段见 spec 6.1）、`AuditResult(StrEnum)`：`SUCCESS="success"` / `DENIED="denied"`
  - `LoginLog`（spec 6.2）、`LoginType(StrEnum)`：`LOCAL="local"` / `FEISHU="feishu"`、`LoginResultEnum(StrEnum)`：`SUCCESS="success"` / `FAILURE="failure"`
  - `SystemMetric`（spec 6.3，含 `sampled_at`）
  - `ExceptionLog`（spec 6.4，含 `fingerprint`、`occurred_at`、`acknowledged`、`acknowledged_by`、`acknowledged_at`）、`ExceptionLevel(StrEnum)`：`ERROR="error"` / `CRITICAL="critical"`
  - `Settings` 新字段（均含默认值）：`monitor_enabled: bool = True`、`monitor_sample_interval_seconds: int = 60`、`monitor_metric_retention_days: int = 30`、`exception_log_retention_days: int = 90`、`login_log_retention_days: int = 180`、`audit_log_retention_days: int = 365`
- 所有模型继承 `app.db.base.Base`；枚举列用与 `app/models/menu.py` 相同的 `Enum(..., values_callable=...)` 写法；`sampled_at`/`occurred_at` 建索引；`audit_log`/`login_log` 的 `created_at` 补建索引；`audit_log` 复合索引 `(user_id, created_at)`、`(resource_type, resource_id)`、`(action, created_at)`。

- [ ] **Step 1: 写失败测试** `tests/test_log_models.py`

```python
async def test_audit_log_roundtrip(session):
    row = AuditLog(user_id=1, actor_username="a", actor_nickname=None,
                   action="user:update", resource_type="user", resource_id=2,
                   result=AuditResult.DENIED, reason="FORBIDDEN",
                   method="PATCH", path="/api/v1/users/2", ip="127.0.0.1",
                   request_id="r1")
    session.add(row); await session.commit()
    loaded = await session.get(AuditLog, row.id)
    assert loaded.result is AuditResult.DENIED and loaded.request_id == "r1"

async def test_login_log_roundtrip(session): ...  # 同风格，断言 LoginType/结果枚举往返

async def test_system_metric_roundtrip(session):
    # cpu_percent=12.5, db_healthy=True, sampled_at=utcnow；断言读回类型与值

async def test_exception_log_roundtrip(session):
    # level=ExceptionLevel.ERROR, acknowledged 默认 False；断言读回

def test_settings_monitor_defaults(test_settings):
    assert test_settings.monitor_sample_interval_seconds == 60
    assert test_settings.audit_log_retention_days == 365
```

- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_log_models.py -v`，Expected: FAIL（ImportError：模块不存在）
- [ ] **Step 3: 实现模型与配置** — 按 Interfaces 落四个模型文件、更新 `app/models/__init__.py`、`Settings` 六个新字段、`.env.example` 六行（`MONITOR_ENABLED=true` 等，带中文注释）。
- [ ] **Step 4: 生成并应用迁移** — Run: `uv run alembic revision --autogenerate -m "create log and monitor tables"`；检查生成脚本含上述全部索引与外键（`audit_log.user_id`/`exception_log.user_id`/`login_log.user_id` 不加数据库级外键，仅普通索引——日志是历史快照，用户删除不应被 RESTRICT 阻塞）；然后对两个库应用：`uv run alembic upgrade head` 与 `DATABASE_URL=mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test uv run alembic upgrade head`
- [ ] **Step 5: 跑测试确认通过** — Run: `uv run pytest tests/test_log_models.py tests/test_db_base.py -v`，Expected: PASS
- [ ] **Step 6: Commit** — `git add app/models app/core/config.py .env.example migrations tests/test_log_models.py && git commit -m "feat: add audit/login/metric/exception log models and config"`

### Task 2: AuthContext 扩展操作人快照

**Files:**
- Modify: `app/core/auth_context.py`, `app/services/auth.py`（`build_auth_context`）
- Test: `tests/test_auth_context.py`

**Interfaces:**
- Produces: `AuthContext` 末尾新增两个带默认值字段 `username: str | None = None`、`nickname: str | None = None`（默认值保证既有全部测试构造不破坏）。`build_auth_context` 两个 return 路径都填充 `username=user.username`、`nickname=user.nickname`。

- [ ] **Step 1: 写失败测试**

```python
def test_auth_context_defaults():
    context = AuthContext(user_id=1, role_id=None, department_id=None,
                          data_scope=None, custom_department_ids=frozenset(),
                          include_children=False, permission_codes=frozenset(),
                          is_super_admin=False)
    assert context.username is None and context.nickname is None

async def test_build_auth_context_carries_actor(session_with_user):
    context = await build_auth_context(session_with_user, user)
    assert context.username == "operator" and context.nickname == "操作员"
```

- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_auth_context.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** — `AuthContext` 追加两个默认字段；`build_auth_context` 的无角色退化分支与正常分支均填充。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_auth_context.py tests/test_user_crud.py tests/test_role_crud.py -v`，Expected: PASS（既有测试不受默认字段影响）
- [ ] **Step 5: Commit** — `git commit -m "feat: carry username/nickname snapshot in AuthContext"`

### Task 3: 请求元信息上下文

**Files:**
- Create: `app/core/request_context.py`
- Modify: `app/core/middleware.py`（`RequestContextMiddleware.dispatch`）
- Test: `tests/test_request_context.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RequestMeta: request_id: str; method: str; path: str; ip: str | None; user_agent: str | None`
  - `def set_request_meta(meta: RequestMeta) -> Token`、`def reset_request_meta(token) -> None`、`def current_request_meta() -> RequestMeta | None`（ContextVar 实现，风格对齐 `app/core/logging.py` 的 request_id 三件套）
- Consumes: `RequestContextMiddleware` 在 `call_next` 前构造 `RequestMeta`（ip 取 `request.client.host`，user_agent 取 `request.headers.get("user-agent")`，截断到 512 字符），在 finally 中 reset。

- [ ] **Step 1: 写失败测试** — 断言：未设置时 `current_request_meta() is None`；set 后取到各字段；reset 后回到 None；复用 `tests/test_request_id.py` 的 app fixture 风格发起真实请求后，中间件设置的 meta 在 handler 内可见（handler 里记录到 `request.state` 再断言）。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_request_context.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** `app/core/request_context.py` 与中间件接入。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_request_context.py tests/test_request_id.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: propagate request meta via ContextVar"`

### Task 4: 审计写入器与 service 装饰器

**Files:**
- Create: `app/core/best_effort.py`, `app/repositories/audit_log.py`, `app/services/audit.py`
- Test: `tests/test_audit_recorder.py`

**Interfaces:**
- Produces:
  - `app/core/best_effort.py`: `async def run_best_effort(factory: Callable[[], Coroutine], *, timeout: float = 2.0, description: str) -> None` — 超时/异常仅 `logger.warning`，绝不抛出。
  - `app/repositories/audit_log.py`: `async def add(session, row: AuditLog) -> AuditLog`（flush 不 commit）、`async def list_audit_logs(session, *, page, page_size, user_id, action, resource_type, resource_id, result, start_time, end_time, keyword) -> tuple[list[AuditLog], int]`
  - `app/services/audit.py`:
    - `DENIED_ERROR_CODES: frozenset[ErrorCode]` = `{CANNOT_MODIFY_SELF, CANNOT_GRANT_SUPER_ADMIN, LAST_SUPER_ADMIN, SYSTEM_ROLE_IMMUTABLE, INVALID_MENU_HIERARCHY, INVALID_DEPT_MOVE, SYSTEM_DEPT_IMMUTABLE}`
    - `def is_denial(error: BusinessError) -> bool` — `http_status == 403` 或 `error.code in DENIED_ERROR_CODES`
    - `async def record_audit_entry(*, action: str, resource_type: str, resource_id: int | None, result: AuditResult, reason: str | None, context: AuthContext | None, request_meta: RequestMeta | None) -> None` — 独立会话 + `run_best_effort` 的唯一写入口，供装饰器与异常处理器共用
    - `def audited(*, action: str, resource_type: str, resource_id_param: str | None = None, resource_id_attr: str | None = None)` — 装饰器；成功记 `AuditResult.SUCCESS`；捕获 `BusinessError` 且 `is_denial` 时经 `record_audit_entry` 记 `DENIED`（`reason=ErrorCode(error.code).name`）并在异常实例上 `setattr(exc, "audit_recorded", True)` 后重抛；非拒绝异常不打标记直接重抛。操作人取 kwargs 里的 `context`（`AuthContext`）——**kwargs 中无 `context` 或其值为 `None` 时，操作人快照字段（user_id/actor_username/actor_nickname）写 NULL，不报错**（Ruling GAP A）；请求元信息取 `current_request_meta()`。
- Consumes: Task 1 模型、Task 2 AuthContext、Task 3 request meta。

- [ ] **Step 1: 写失败测试**

```python
async def test_success_audit_written(session, ...):
    @audited(action="user:update", resource_type="user", resource_id_param="user_id")
    async def op(session, *, context, user_id): return "ok"
    await op(session, context=SUPER_ADMIN_CONTEXT, user_id=7)
    row = (await session.scalars(select(AuditLog))).one()
    assert row.result is AuditResult.SUCCESS and row.resource_id == 7
    assert row.actor_username == SUPER_ADMIN_CONTEXT.username

async def test_denied_audit_survives_rollback(session, ...):
    @audited(action="user:update", resource_type="user", resource_id_param="user_id")
    async def op(session, *, context, user_id):
        session.add(User(username="ghost", ...))  # 制造待回滚变更
        raise BusinessError(ErrorCode.LAST_SUPER_ADMIN, "last", http_status=403)
    with pytest.raises(BusinessError) as error_info: await op(session, context=..., user_id=7)
    await session.rollback()
    row = (await session.scalars(select(AuditLog))).one()
    assert row.result is AuditResult.DENIED and row.reason == "LAST_SUPER_ADMIN"
    assert getattr(error_info.value, "audit_recorded", False) is True  # 标记打在抛出的异常实例上

async def test_not_denial_not_recorded(...):
    # BusinessError(USER_NOT_FOUND, 404) 不产生 audit 行，也不打标记

async def test_write_failure_does_not_propagate(...):
    # 用 monkeypatch 让独立会话抛错，断言被装饰函数正常返回/原异常不受影响
```

- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_audit_recorder.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** 三个文件。装饰器用 `functools.wraps`；审计写入与业务会话完全隔离（新 `session_factory()` 会话内 add+commit，整体包在 `run_best_effort` 里）。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_audit_recorder.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: independent-transaction audit recorder and service decorator"`

### Task 5: 依赖层 403 兜底记录 + 去重 + 路由审计注册表

**Files:**
- Create: `app/core/audit_meta.py`
- Modify: `app/core/exception_handlers.py`（`business_error_handler`）
- Test: `tests/test_audit_denial_hooks.py`

**Interfaces:**
- Produces:
  - `app/core/audit_meta.py`: `@dataclass(frozen=True) class RouteAuditMeta: action: str; resource_type: str`；`def register_audit_route(method: str, path: str, *, action: str, resource_type: str) -> None`；`def get_audit_route_meta(method: str, route_path: str) -> RouteAuditMeta | None`（`route_path` 为 FastAPI 路径模板，如 `/users/{user_id}`）
  - `business_error_handler` 行为：`is_denial(exc)` 且未带 `audit_recorded` 标记且 `get_audit_route_meta(request.method, request.scope["route"].path)` 命中时，经 `record_audit_entry` 记 `DENIED` 行（`resource_id` 从路径参数解析 int，失败为 NULL），记录前同样打上 `audit_recorded` 标记。
- Consumes: Task 4 的 `is_denial`、标记约定、独立会话写入（从 `app.services.audit` 导出 `record_audit_entry(...) -> None` 供两处钩子复用，参数：`action, resource_type, resource_id, result, reason, context, request_meta`）。

- [ ] **Step 1: 写失败测试**（用 `httpx.ASGITransport` 打真实 app，路由注册表在测试内注册到一条测试路由上）
  - `test_service_denial_recorded_once`：service 内 403 → 全表恰 1 条 denied。
  - `test_dependency_denial_recorded`：`require_permission` 抛 403（service 未运行）→ 恰 1 条 denied，`resource_id` 取自路径。
  - `test_non_denial_not_recorded`：404 业务异常 → 无 audit 行。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_audit_denial_hooks.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** `audit_meta.py` 与 `business_error_handler` 接入。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_audit_denial_hooks.py tests/test_response_and_errors.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: dependency-level denial audit hook with dedup registry"`

### Task 6: 用户模块接入审计

**Files:**
- Modify: `app/services/user.py`（`create_local_user` 增加可选 `context` 入参；全部写用例加 `@audited`）、`app/api/users.py`（create 传 context）
- Test: `tests/test_user_audit.py`

**Interfaces:**
- Produces: 各用例的 `action`/`resource_type`/id 来源对照——`create_local_user`: `user:create`/`user`/`resource_id_attr="id"`；`update_user`: `user:update`/`user_id`；`delete_user`: `user:delete`/`user_id`；`assign_user_role`: `user:assign`/`user_id`；`assign_user_department`: `user:assign`/`user_id`；`update_user_status`: `user:update`/`user_id`；`reset_user_password`: `user:update`/`user_id`。
- **Ruling GAP A（必守）**：`create_local_user` 的新签名必须是 `async def create_local_user(session, payload, *, context: AuthContext | None = None) -> User`。既有 ~34 处测试直调不传 context，必须继续可运行（装饰器对缺失 context 写 NULL 操作人）。API 路由 `POST /users` 始终传 `context`。
- Consumes: Task 4 装饰器、Task 5 注册表（`app/api/users.py` 底部为上述写路由调用 `register_audit_route`，path 用 FastAPI 模板如 `/users/{user_id}`）。

- [ ] **Step 1: 写失败测试** — service 直调：成功创建产生 `user:create` success 行且 `resource_id` 为新 id；`assign_user_role` 自我修改抛 `CANNOT_MODIFY_SELF` 后恰 1 条 denied。API 级：无 `user:create` 权限调用 POST /users → 依赖层 403 恰 1 条 denied。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_user_audit.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** — 按对照表加装饰器与注册；`create_local_user(session, payload, *, context)`，API 侧传 `_context`。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_user_audit.py tests/test_user_crud.py tests/test_privilege_escalation.py -v`，Expected: PASS（既有签名调用在测试内已传 context 的保持兼容：`create_local_user` 的 `context` 设为仅关键字参数且必填，测试内同步补传）
- [ ] **Step 5: Commit** — `git commit -m "feat: audit user management write use cases"`

### Task 7: 角色/菜单/部门模块接入审计

**Files:**
- Modify: `app/services/role.py`, `app/services/menu.py`, `app/services/department.py`, `app/api/roles.py`, `app/api/menus.py`, `app/api/departments.py`
- Test: `tests/test_config_resource_audit.py`

**Interfaces:**
- Produces 对照表——role：`role:create`/`role:update`/`role:delete`/`role:assign`（`PUT /roles/{id}/menus` 与 `PUT /roles/{id}/data-scope` 均记 `role:assign`，id 取 `role_id`）；menu：`menu:create`/`menu:update`/`menu:delete`；department：`dept:create`/`dept:update`/`dept:move`（PATCH `/{id}/move` 路由）/`dept:delete`。`resource_type` 分别为 `role`/`menu`/`department`。
- Consumes: Task 4/5 同一套机制；各写 service 已有 `context` 入参（核对现状，若无则补）。

- [ ] **Step 1: 写失败测试** — 每模块各 1 条 success（API 级或 service 级）+ 至少 1 条 denied（如 `SYSTEM_ROLE_IMMUTABLE`、`INVALID_DEPT_MOVE`），断言 action/resource_type/reason 正确。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_config_resource_audit.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** — 加装饰器与路由注册。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_config_resource_audit.py tests/test_role_crud.py tests/test_menu_crud.py tests/test_department_crud.py tests/test_department_move.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: audit role/menu/department write use cases"`

### Task 8: 登录日志

**Files:**
- Create: `app/repositories/login_log.py`, `app/services/login_log.py`
- Modify: `app/services/auth.py`（`login_local_user`、`login_with_feishu` 改为外层包装函数，内部实现改名 `_login_local_user`/`_login_with_feishu`）
- Test: `tests/test_login_log.py`

**Interfaces:**
- Produces:
  - `app/repositories/login_log.py`: `add`、`list_login_logs(session, *, page, page_size, user_id, username, login_type, result, ip, start_time, end_time)`（返回 `(rows, total)`）
  - `app/services/login_log.py`: `async def record_login_log(*, user_id, username, login_type, result, reason, request_meta, device_id=None) -> None`（独立会话 + `run_best_effort`）
  - 包装语义（spec 7 节）：包装整个登录调用；返回 `LoginResult` 记 success；`BusinessError` 记 failure（`reason=ErrorCode(error.code).name`）重抛；其他异常记 failure + `reason="INTERNAL_ERROR"` 后重抛。`username` 对飞书登录取 `info.open_id` 之外不落用户名——落 `user.nickname` 不合适，落 `username=None`、`user_id` 取登录到的用户。
- Consumes: Task 1 模型、Task 3 request meta。

- [ ] **Step 1: 写失败测试** — 复用 `tests/test_local_auth.py` / `tests/test_feishu_login.py` 的夹具风格：本地成功 1 条 success；凭据错误 1 条 failure `reason=INVALID_CREDENTIALS`；账号停用 → `ACCOUNT_DISABLED`；限流 → `RATE_LIMITED`；飞书成功与上游 502（mock `FeishuClient.exchange_code` 抛 `FEISHU_UPSTREAM_ERROR`）各 1 条。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_login_log.py -v`，Expected: FAIL
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_login_log.py tests/test_local_auth.py tests/test_feishu_login.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: record login attempts to login_log"`

### Task 9: 异常日志记录

**Files:**
- Create: `app/services/exception_log.py`, `app/repositories/exception_log.py`
- Modify: `app/core/exception_handlers.py`（`unhandled_exception_handler`、`http_exception_handler` 5xx 分支、`business_error_handler` 5xx 分支）
- Test: `tests/test_exception_log.py`

**Interfaces:**
- **Ruling NOTE C（必守）**：`business_error_handler` 在 Task 5 已加入"拒绝审计钩子"。本任务只**新增** 5xx 异常记录分支，必须保留既有 `is_denial` + `audit_recorded` 去重逻辑——修改后 service 层 403 仍恰好 1 条 denied、依赖层 403 仍恰好 1 条 denied。改完须回跑 `tests/test_audit_denial_hooks.py` 确认未回归。
- Produces:
  - `app/services/exception_log.py`: `def build_fingerprint(*, exception_type: str, message: str, traceback_text: str) -> str`（`sha256(type + 归一化消息(数字串替换为`N`) + 栈顶帧)前 64 位十六进制`）；`async def record_exception(*, exc, level, request_meta, user_id, status_code) -> None`（独立会话 + `run_best_effort`，message 经 `mask_sensitive_text` 且截断 1024）
  - `app/repositories/exception_log.py`: `add`、`list_exceptions(session, *, page, page_size, exception_type, level, acknowledged, start_time, end_time)`、`summary_by_fingerprint(session, *, start_time, end_time) -> list[Row]`（按 fingerprint 聚合：次数、最近时间、全部 acknowledged 与否）、`get_by_id`、`ack(session, row, *, user_id, at)`
- Consumes: Task 1 模型、Task 3 request meta、既有 `current_request_id()`。

- [ ] **Step 1: 写失败测试**（httpx 打真实 app，注册一条抛 `RuntimeError("boom 123")` 的测试路由）
  - 未捕获异常 → 1 行 `exception_type="RuntimeError"`、`message` 含 boom、`status_code=500`。
  - `test_exception_message_masked`：异常消息含 `password=secret123` → 落库为 `***`。
  - HTTPException 503 → 1 行；`BusinessError(http_status=502)` → 1 行；`BusinessError(404)` → 0 行。
  - 相同消息形状（`boom 456`）两次 → `build_fingerprint` 相同。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_exception_log.py -v`，Expected: FAIL
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_exception_log.py tests/test_response_and_errors.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: capture server exceptions into exception_log"`

### Task 10: 系统监控采集与接口

**Files:**
- Modify: `pyproject.toml`（新增 `psutil`）、`app/api/health.py`（探活抽出）、`app/main.py`（lifespan 启停采样任务）、`app/api/router.py`（**Ruling GAP B**：挂载 `monitor_router`，否则接口 404）
- Create: `app/services/monitor.py`, `app/services/health.py`, `app/api/monitor.py`, `app/schemas/monitor.py`
- Test: `tests/test_system_monitor.py`

**Interfaces:**
- Produces:
  - `app/services/health.py`: `async def check_database(session) -> bool`、`async def check_redis(client) -> bool`（异常吞掉返回 False）；`app/api/health.py` 改为复用，行为不变。
  - `app/services/monitor.py`:
    - `def collect_psutil_snapshot() -> dict`（cpu_percent(interval=0.1)、内存/根分区用量、`process_memory_mb=Process().memory_info().rss / 1MB`）
    - `async def sample_once(session) -> SystemMetric`（psutil + 双探活，写一行）
    - `async def run_sampler(interval_seconds: int) -> None`（循环 sample_once 用独立会话；`asyncio.sleep(interval)`；`CancelledError` 直接退出——供 lifespan task 取消）
  - `app/schemas/monitor.py`: `SystemMetricRead`、`CurrentSnapshotRead`（快照字段 + `db_healthy`/`redis_healthy` + `sampled_at`）
  - `app/api/monitor.py`（prefix `/monitor/system`，tag `系统监控`，依赖 `require_permission("monitor:read")`）:
    - `GET /monitor/system/current` → psutil 即时采集 + 双探活，不落库
    - `GET /monitor/system/history` → 分页（时间倒序），筛选 `start_time`/`end_time`
  - `app/main.py` lifespan：`settings.monitor_enabled` 时 `asyncio.create_task(run_sampler(...))` 存入 `application.state`，关闭时 `task.cancel()` + `await asyncio.gather(task, return_exceptions=True)`。
- Consumes: Task 1 模型/配置。

- [ ] **Step 1: 写失败测试**
  - `test_sample_once_writes_row`：调用后表内 1 行，`cpu_percent` 在 0~100，`db_healthy` 为 bool。
  - `test_sampler_task_cancelled_on_shutdown`：`asyncio.wait_for(run_sampler(3600), timeout=1)` 抛 `TimeoutError` 后任务可被 cancel 且不抛其他异常（证明长循环可干净取消）。
  - `test_current_snapshot_endpoint`：带 `monitor:read` 权限的 context 请求返回 200，`data` 含 `cpu_percent`/`db_healthy`；无权限返回 403。
  - `test_history_endpoint_filters_and_pages`。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_system_monitor.py -v`，Expected: FAIL
- [ ] **Step 3: 实现** — `uv add psutil`；探活抽取时保持 `tests/test_health.py` 语义不变。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_system_monitor.py tests/test_health.py tests/test_app_boot.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: system metric sampling with live snapshot and history APIs"`

### Task 11: 操作/登录日志查询接口

**Files:**
- Create: `app/api/logs.py`, `app/schemas/logs.py`
- Modify: `app/api/router.py`
- Test: `tests/test_log_query_apis.py`

**Interfaces:**
- Produces:
  - `app/schemas/logs.py`: `AuditLogRead`、`LoginLogRead`（时间字段序列化为 ISO8601 带时区，风格对齐 `app/schemas/user.py`）
  - `app/api/logs.py`:
    - `GET /logs/operations`（`audit:read`），筛选 `user_id`/`action`/`resource_type`/`resource_id`/`result`/`start_time`/`end_time`/`keyword`（keyword 匹配 actor_username/actor_nickname），按 `created_at` 倒序
    - `GET /logs/logins`（`login_log:read`），筛选 `user_id`/`username`/`login_type`/`result`/`ip`/`start_time`/`end_time`，倒序
  - `app/api/router.py` 挂载 logs_router（**保留 Task 10 已挂载的 monitor_router**）。
- Consumes: Task 4/8 的 repository `list_*` 函数。

- [ ] **Step 1: 写失败测试** — 预置若干行后：分页形状；`action`、`result`、时间范围各筛选 1 例；`keyword` 命中昵称；无权限 403；空结果返回 `items=[] total=0`。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_log_query_apis.py -v`，Expected: FAIL
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_log_query_apis.py tests/test_user_crud.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: operation and login log query APIs"`

### Task 12: 异常查询与 ack 接口

**Files:**
- Modify: `app/api/monitor.py`
- Test: `tests/test_exception_apis.py`

**Interfaces:**
- Produces（挂到 `/monitor/exceptions`，tag `异常监控`）:
  - `GET /monitor/exceptions`（`exception:read`）：分页，筛选 `exception_type`/`level`/`acknowledged`/`start_time`/`end_time`，按 `occurred_at` 倒序
  - `GET /monitor/exceptions/summary`（`exception:read`）：按 fingerprint 分组，条目含 `fingerprint`、`exception_type`、`count`、`last_occurred_at`、`level`、`acknowledged`（组内全部已处理才为 true）
  - `POST /monitor/exceptions/{id}/ack`（`exception:update`）：置 `acknowledged=true`、`acknowledged_by=context.user_id`、`acknowledged_at=utcnow`；404 时返回 `NOT_FOUND` 业务错误；幂等（重复 ack 不改时间）
  - schemas：`ExceptionLogRead`、`ExceptionSummaryRead` 加入 `app/schemas/monitor.py`
- Consumes: Task 9 的 repository 与 service。

- [ ] **Step 1: 写失败测试** — 预置 3 行（2 行同指纹）：summary 分组 count=2 与 acknowledged 语义；list 的 `acknowledged=false` 筛选；ack 后单行状态、处理人正确；重复 ack 幂等；ack 不存在 id → 404；`exception:read` 缺失 → 403。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_exception_apis.py -v`，Expected: FAIL
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_exception_apis.py tests/test_exception_log.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: exception list, fingerprint summary and ack APIs"`

### Task 13: 菜单种子、保留清理与收尾

**Files:**
- Modify: `scripts/init_data.py`, `app/services/monitor.py`（清理）, `app/main.py`（清理任务挂到采样循环）, `tests/test_seed_data.py`
- Test: `tests/test_retention.py`（新）, `tests/test_seed_data.py`（扩充）

**Interfaces:**
- Produces:
  - `app/services/monitor.py`: `async def run_retention(session) -> dict[str, int]` — 对四表按 spec 第 8 节的时间列删除超过保留天数的行，返回各表删除数；对应保留天数配置为 `0` 时跳过该表。采样循环每约 1 小时调用一次（按 `sampled_at` 与 `time.monotonic()` 计数均可，实现者裁量，但必须可测：直接暴露 `run_retention`）。
  - 菜单种子新增（`_MENU_SEEDS` 追加，父级 `system`）：`logs`（目录 `/system/logs`）、`logs-operations`（菜单 `/system/logs/operations`，`audit:read`）、`logs-logins`（菜单 `/system/logs/logins`，`login_log:read`）、`monitor`（菜单 `/system/monitor`，`monitor:read`）、`exceptions`（菜单 `/system/monitor/exceptions`，`exception:read`）、`exceptions-ack`（按钮，父级 `exceptions`，`exception:update`）。
- Consumes: Task 1 配置、Task 10 采样循环。

- [ ] **Step 1: 写失败测试**
  - `test_retention_deletes_only_expired`：各表插入 1 行 10 天前 + 1 行刚刚，把该表保留天数设为 **7 天**（10 > 7，故旧行应被删、新行保留）执行后只剩新行。（注：保留语义是"删除早于 N 天的行"，用例的保留天数必须小于插入行的年龄，否则旧行本就不该删。）
  - `test_retention_zero_disables_cleanup`：配置为 0 时旧行仍在、返回计数 0。
  - `test_seed_data.py`：`EXPECTED_PERMISSION_CODES` 增加 5 个新权限码；断言 `exceptions-ack` 按钮父级为 `exceptions`。
- [ ] **Step 2: 跑测试确认失败** — Run: `uv run pytest tests/test_retention.py tests/test_seed_data.py -v`，Expected: FAIL
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑测试确认通过** — Run: `uv run pytest tests/test_retention.py tests/test_seed_data.py tests/test_system_monitor.py -v`，Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: monitor menu seeds and configurable log retention"`

### Task 14: 全量回归与质量门

**Files:**
- Modify: 无（如回归暴露问题，修复归属对应任务文件）
- Test: 全量

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1: 全量测试** — Run: `uv run pytest -v`，Expected: 全绿
- [ ] **Step 2: Lint 与类型** — Run: `uv run ruff check app tests scripts && uv run ruff format --check app tests scripts && uv run mypy app`，Expected: 无错误
- [ ] **Step 3: pre-commit** — Run: `uv run pre-commit run --all-files`，Expected: 通过
- [ ] **Step 4: 迁移可回滚验证** — Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`（测试库），Expected: 无错误
- [ ] **Step 5: Commit** — `git commit -m "test: full regression green for monitoring and logging" --allow-empty`（若 Step 1–4 有修复则随修复提交）
