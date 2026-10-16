# 用户与权限管理后端 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 30 人规模的团队内部后台实现用户、角色、菜单、部门四块能力的中台后端，身份认证接飞书，权限分功能权限与部门数据权限两层。

**Architecture:** FastAPI 三层结构（api → services → repositories → models），MySQL 存业务数据，Redis 存会话与权限缓存。菜单是一棵树，`type` 区分目录/菜单/按钮，同时供前端路由与按钮权限使用。部门用物化路径 `path` 实现子树查询，数据权限统一由一个过滤函数生成，防提权规则集中在 service 层。

**Tech Stack:** Python 3.12+（实机 3.14）、FastAPI、SQLAlchemy 2.0 async + asyncmy、Alembic、MySQL 8.4、Redis 8、Pydantic v2 / pydantic-settings、PyJWT、argon2-cffi、uv、ruff、mypy、pytest + pytest-asyncio + httpx。

**Spec:** [docs/superpowers/specs/2026-10-16-user-permission-backend-design.md](../specs/2026-10-16-user-permission-backend-design.md)

## Global Constraints

- 语言与框架：Python + FastAPI；标识符、表名、字段名用英文，注释与文档用中文。
- 实机 Python 为 3.14；若某依赖尚不支持，回退并在 `pyproject.toml` 锁定 3.12。
- 数据库 MySQL 8，字符集 `utf8mb4`，表与列 snake_case。
- 时间统一以 UTC 存 `DATETIME(6)`，API 返回 ISO8601 带时区。
- 接口前缀 `/api/v1`；成功响应 `{"code": 0, "message": "ok", "data": ...}`。
- 分页统一 `{"items": [], "total": 0, "page": 1, "page_size": 20}`，`page_size` 有上限。
- 业务错误码分段：`1xxx` 通用/认证，`2xxx` 用户，`3xxx` 角色，`4xxx` 菜单，`5xxx` 部门。
- 权限码格式 `资源:动作`，动作取 `create` / `read` / `update` / `delete` / `assign`。
- `access_token` 有效期 30 分钟，`refresh_token` 有效期 7 天并轮换。
- Redis 键名统一 `{app}:{env}:{domain}:{ident}`，所有键必须设 TTL。
- 依赖方向单向：`api → services → repositories → models`，禁止反向引用，禁止路由内直接写 SQL。
- 事务边界在 service 层，repository 不自行 `commit`。
- 密钥一律经环境变量注入，`.env` 与任何密钥不入库；`.env.example` 入库。
- 权限校验一律在后端完成，前端隐藏按钮不作为安全边界。
- 审计日志、i18n、幂等键、全局软删除、消息队列本期不做。

## Review Focus

以下是最容易被忽略、但一旦出错会伤到用户的输入或情形，每条在本计划中都指定了负责它的任务与测试：

1. **把部门移动到自己或自己的后代下面** — 会形成环，`path` 前缀查询退化为无限/错误匹配；Task 6 必须拒绝并返回明确错误。
2. **新用户或未分配用户（`department_id` 为空）的数据范围** — 期望看不到任何部门数据且不报错；Task 10 覆盖"待分配"节点与空部门两种输入。
3. **停用角色 / 停用菜单 / 停用部门之后的登录与过滤** — 期望按 7.5 节语义分别退化为空权限、隐藏路由、禁止新分配但保留历史归属；Task 7、Task 8、Task 13 各覆盖一条。
4. **权限或状态变更后的即时失效** — 期望旧 token 立刻失效、权限缓存立刻重建，而不是等 30 分钟；Task 11、Task 12、Task 13 覆盖。
5. **并发或误操作导致系统超管归零** — 期望"至少保留一个可用超管"的约束在 service 层被强制执行；Task 13 覆盖。

---

### Task 1: 工程脚手架与配置

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `app/__init__.py`, `app/core/__init__.py`
- Create: `app/core/config.py`
- Create: `app/main.py`
- Create: `tests/__init__.py`, `tests/conftest.py`
- Create: `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: 无。
- Produces: `app.core.config.settings`（`Settings` 单例，字段见下）；`app.main.app`（FastAPI 实例）。

- [ ] **Step 1: 写 `pyproject.toml`**

声明项目元数据与依赖，并配置 ruff / mypy / pytest。依赖至少包含：`fastapi`、`uvicorn[standard]`、`sqlalchemy[asyncio]>=2.0`、`asyncmy`、`alembic`、`redis`、`pydantic>=2`、`pydantic-settings`、`pyjwt`、`argon2-cffi`、`httpx`；开发依赖 `pytest`、`pytest-asyncio`、`ruff`、`mypy`、`pre-commit`。

- [ ] **Step 2: 写 `app/core/config.py`**

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "tianxiadmin"
    env: Literal["local", "test", "prod"] = "local"
    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    cors_origins: list[str] = []
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_redirect_uri: str = ""
    initial_superadmin_username: str = "superadmin"
    initial_superadmin_password: str = ""

settings = Settings()
```

`database_url`、`redis_url`、`jwt_secret` 无默认值，缺失时启动即失败（fail-fast）。

- [ ] **Step 3: 写 `.env.example` 与 `.gitignore`**

`.env.example` 列出全部配置项与占位值。`.gitignore` 至少忽略 `.env`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`。

- [ ] **Step 4: 写 `app/main.py`**

```python
def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name)
    ...
    return app

app = create_app()
```

用 `lifespan` 管理启动与优雅停机：启动时校验关键配置并初始化 Redis 连接，停机时关闭 Redis 客户端并释放数据库连接池。OpenAPI 按业务域打 tag（认证、用户、角色、菜单、部门、健康），使 `/docs` 可作为接口契约查阅。

- [ ] **Step 5: 写 `tests/conftest.py`**

提供 `test_settings` fixture，用 `Settings(...)` 显式注入测试用 `database_url` / `redis_url` / `jwt_secret`，避免测试读取真实 `.env`。

- [ ] **Step 6: 写测试 `tests/test_app_boot.py`**

```python
async def test_app_boots_and_serves_openapi():
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
```

- [ ] **Step 7: 安装依赖并运行测试**

Run: `uv sync && uv run pytest tests/test_app_boot.py -v`
Expected: PASS

- [ ] **Step 8: 运行静态检查**

Run: `uv run ruff check . && uv run mypy app`
Expected: 无错误

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .env.example .gitignore .pre-commit-config.yaml app tests
git commit -m "chore: bootstrap FastAPI project with config and tooling"
```

---

### Task 2: 数据库与 Redis 基础设施 + 健康检查

**Files:**
- Create: `app/db/__init__.py`, `app/db/base.py`, `app/db/session.py`
- Create: `app/core/redis.py`
- Create: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`
- Create: `app/api/__init__.py`, `app/api/health.py`
- Modify: `app/main.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `settings`（Task 1）。
- Produces: `app.db.base.Base`（ORM 声明基类，含 `id`、`created_at`、`updated_at`）；`app.db.session.get_session`（FastAPI 依赖，`AsyncGenerator[AsyncSession, None]`）；`app.core.redis.get_redis`（依赖，返回 `Redis`）；`app.core.redis.redis_client`（全局客户端）。

- [ ] **Step 1: 写 `app/db/base.py`**

`Base` 用 `DeclarativeBase`；`id` 为 `BigInteger` 主键自增；`created_at`、`updated_at` 为 `DateTime(timezone=False)`，`server_default=func.now()`，`updated_at` 带 `onupdate`。

- [ ] **Step 2: 写 `app/db/session.py`**

`create_async_engine(settings.database_url, pool_pre_ping=True, pool_recycle=1800, pool_size=10, max_overflow=20)`；`async_sessionmaker(expire_on_commit=False)`；`get_session` 用 `async with` + `yield`。

- [ ] **Step 3: 写 `app/core/redis.py`**

```python
redis_client = Redis.from_url(settings.redis_url, decode_responses=True)

def key(*parts: str) -> str:
    return ":".join([settings.app_name, settings.env, *parts])
```

- [ ] **Step 4: 配置 Alembic**

`migrations/env.py` 使用 async engine，从 `app.db.base.Base.metadata` 读取 `target_metadata`，URL 取自 `settings.database_url`。运行 `uv run alembic init` 的产物按需改写。

- [ ] **Step 5: 写 `app/api/health.py`**

```python
@router.get("/health/live")
async def liveness() -> dict: ...

@router.get("/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict: ...
```

`ready` 执行 `SELECT 1` 并 `await redis_client.ping()`；任一失败返回 `503`。

- [ ] **Step 6: 写测试 `tests/test_health.py`**

```python
async def test_liveness_ok(): ...  # 200

async def test_readiness_ok_when_db_and_redis_up(): ...  # 200

async def test_readiness_503_when_redis_down(monkeypatch): ...  # 503
```

- [ ] **Step 7: 运行测试**

Run: `uv run pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 8: 验证迁移可执行**

Run: `uv run alembic upgrade head`
Expected: 无错误（此时无业务表，仅验证链路）

- [ ] **Step 9: Commit**

```bash
git add app/db app/core/redis.py app/api alembic.ini migrations app/main.py tests/test_health.py
git commit -m "feat: add database, redis infrastructure and health endpoints"
```

---

### Task 3: 统一响应、错误码与全局异常处理

**Files:**
- Create: `app/core/errors.py`
- Create: `app/core/response.py`
- Create: `app/core/exception_handlers.py`
- Modify: `app/main.py`
- Test: `tests/test_response_and_errors.py`

**Interfaces:**
- Consumes: `app.main.create_app`（Task 1）。
- Produces: `BusinessError(code: int, message: str, http_status: int = 400)`；`success(data=None, message="ok") -> dict[str, Any]`；`Page[T]`（泛型模型，字段 `items` / `total` / `page` / `page_size`）；`register_exception_handlers(app: FastAPI) -> None`；错误码常量模块 `ErrorCode`。

- [ ] **Step 1: 定义错误码常量**

```python
class ErrorCode(IntEnum):
    UNAUTHENTICATED = 1000
    INVALID_CREDENTIALS = 1001
    ACCOUNT_DISABLED = 1002
    TOKEN_EXPIRED = 1003
    TOKEN_INVALID = 1004
    RATE_LIMITED = 1005
    FORBIDDEN = 1010

    USER_NOT_FOUND = 2000
    USERNAME_EXISTS = 2001
    CANNOT_MODIFY_SELF = 2002
    CANNOT_GRANT_SUPER_ADMIN = 2003
    LAST_SUPER_ADMIN = 2004

    ROLE_NOT_FOUND = 3000
    SYSTEM_ROLE_IMMUTABLE = 3001
    ROLE_IN_USE = 3002

    MENU_NOT_FOUND = 4000
    INVALID_MENU_HIERARCHY = 4001
    PERMISSION_CODE_EXISTS = 4002

    DEPT_NOT_FOUND = 5000
    DEPT_HAS_CHILDREN = 5001
    DEPT_HAS_USERS = 5002
    INVALID_DEPT_MOVE = 5003
    SYSTEM_DEPT_IMMUTABLE = 5004
```

- [ ] **Step 2: 写 `app/core/errors.py`**

```python
class BusinessError(Exception):
    def __init__(self, code: ErrorCode, message: str, http_status: int = 400) -> None: ...
```

- [ ] **Step 3: 写 `app/core/response.py`**

```python
def success(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data}

class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
```

- [ ] **Step 4: 写 `app/core/exception_handlers.py`**

注册四类 handler：`BusinessError`（按 `http_status` 返回 `{"code", "message", "request_id"}`）、`RequestValidationError`（400 + `1000`≤... 用参数错误码，HTTP 422）、`HTTPException`、`Exception`（500，生产环境不返回堆栈）。

- [ ] **Step 5: 写测试 `tests/test_response_and_errors.py`**

```python
async def test_business_error_returns_envelope(): ...  # 403 + code
async def test_validation_error_returns_422_without_stack(): ...
async def test_unhandled_exception_returns_500_without_stack(): ...
```

- [ ] **Step 6: 运行测试**

Run: `uv run pytest tests/test_response_and_errors.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/core tests/test_response_and_errors.py
git commit -m "feat: add unified response envelope and error handling"
```

---

### Task 4: 请求追踪与结构化日志

**Files:**
- Create: `app/core/logging.py`
- Create: `app/core/middleware.py`
- Modify: `app/main.py`
- Test: `tests/test_request_id.py`

**Interfaces:**
- Consumes: `app.main.create_app`（Task 1）。
- Produces: `RequestContextMiddleware`（注入 `request_id`、回写 `X-Request-Id`、记录访问日志）；`configure_logging(env: str) -> None`；`current_request_id() -> str`；`configure_security(app: FastAPI) -> None`（CORS 白名单 + 安全响应头）。

- [ ] **Step 1: 写 `app/core/logging.py`**

本地环境用可读格式，非本地用 JSON 格式。提供 `SENSITIVE_KEYS = {"password", "token", "access_token", "refresh_token", "code", "authorization"}` 与脱敏函数。

- [ ] **Step 2: 写 `app/core/middleware.py`**

中间件读取入站 `X-Request-Id`，无则生成 UUID4；用 `contextvars` 保存；响应头回写；记录 `method`、`path`、`status_code`、`duration_ms`、`user_id`（未认证为 `null`）。

同时实现 `configure_security(app)`：CORS 来源取 `settings.cors_origins` 白名单（不使用 `*` 配合凭证），并统一添加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer` 等安全响应头。

- [ ] **Step 3: 写测试 `tests/test_request_id.py`**

```python
async def test_response_has_request_id_header(): ...
async def test_inbound_request_id_is_preserved(): ...
async def test_sensitive_fields_are_masked_in_logs(caplog): ...
async def test_cors_allows_whitelisted_origin_only(): ...
async def test_security_headers_present_on_response(): ...
```

- [ ] **Step 4: 运行测试**

Run: `uv run pytest tests/test_request_id.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/logging.py app/core/middleware.py app/main.py tests/test_request_id.py
git commit -m "feat: add request tracing, structured logging and security headers"
```

---

### Task 5: 部门领域模型与树 CRUD

**Files:**
- Create: `app/models/__init__.py`, `app/models/department.py`
- Create: `app/schemas/department.py`
- Create: `app/repositories/department.py`
- Create: `app/services/department.py`
- Create: `app/api/departments.py`
- Modify: `app/main.py`
- Create: `migrations/versions/<rev>_create_department.py`
- Test: `tests/test_department_crud.py`

**Interfaces:**
- Consumes: `Base`、`get_session`（Task 2）；`success`、`Page`、`BusinessError`、`ErrorCode`（Task 3）。
- Produces: `Department` ORM（字段见 spec 7.2，含 `path`、`is_system`）；`create_department(session, payload) -> Department`；`get_tree(session, include_disabled: bool = False) -> list[DepartmentNode]`；`update_department(session, dept_id, payload) -> Department`；`delete_department(session, dept_id) -> None`；`list_subtree_ids(session, dept_id) -> list[int]`。

- [ ] **Step 1: 写 `app/models/department.py`**

字段：`id`、`parent_id`（`ForeignKey("department.id")`，可空，带索引）、`name`、`path`（`String(512)`，索引）、`sort_order`、`status`（枚举 `active`/`disabled`）、`is_system`。`path` 存形如 `/1/3/7/`。

- [ ] **Step 2: 生成迁移**

Run: `uv run alembic revision --autogenerate -m "create department"`
检查生成的脚本含 `path` 索引与 `parent_id` 外键，然后 `uv run alembic upgrade head`。

- [ ] **Step 3: 写 `app/schemas/department.py`**

`DepartmentCreate(name, parent_id: int | None, sort_order: int = 0)`；`DepartmentUpdate`（全可选）；`DepartmentNode`（含 `children: list[DepartmentNode]`，用 `model_rebuild()` 处理自引用）。

- [ ] **Step 4: 写测试 `tests/test_department_crud.py`（先失败）**

```python
async def test_create_root_department_sets_path(): ...
async def test_create_child_department_appends_parent_path(): ...
async def test_get_tree_nests_children(): ...
async def test_delete_department_with_children_returns_5001(): ...
async def test_delete_department_with_users_returns_5002(): ...
async def test_system_department_cannot_be_renamed_or_deleted(): ...  # 5004
async def test_create_child_under_disabled_parent_is_rejected(): ...
```

- [ ] **Step 5: 运行测试确认失败**

Run: `uv run pytest tests/test_department_crud.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 6: 实现 repository 与 service**

`create_department` 先落库拿到 `id`，再回写 `path = parent.path + f"{id}/"`（根节点为 `/{id}/`），同一事务提交。`delete_department` 依次校验：`is_system`、存在子部门、存在用户。

- [ ] **Step 7: 实现 `app/api/departments.py` 并挂载**

`GET /departments/tree`、`POST /departments`、`PATCH /departments/{id}`、`DELETE /departments/{id}`，全部经 `success()` 包装。权限码 `dept:*` 的依赖在 Task 15 接入，本任务先留占位。

- [ ] **Step 8: 运行测试至通过**

Run: `uv run pytest tests/test_department_crud.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add app/models app/schemas/department.py app/repositories app/services/department.py app/api/departments.py migrations tests/test_department_crud.py
git commit -m "feat: add department model and tree CRUD"
```

---

### Task 6: 部门树移动与 path 级联

**Files:**
- Modify: `app/services/department.py`
- Modify: `app/api/departments.py`
- Test: `tests/test_department_move.py`

**Interfaces:**
- Consumes: `Department`、`list_subtree_ids`、`BusinessError`（Task 5）。
- Produces: `move_department(session, dept_id: int, new_parent_id: int | None) -> Department`。

- [ ] **Step 1: 写测试 `tests/test_department_move.py`（先失败）**

```python
async def test_move_to_self_returns_5003(): ...
async def test_move_to_own_descendant_returns_5003(): ...
async def test_move_updates_own_path(): ...
async def test_move_cascades_path_to_all_descendants(): ...
async def test_move_system_department_returns_5004(): ...
async def test_move_to_root_sets_path_from_own_id(): ...
```

级联测试的固定树：`A(1) → B(2) → C(3)`，把 `B` 移到根下，断言 `C.path` 从 `/1/2/3/` 变为 `/2/3/`。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_department_move.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `move_department`**

校验顺序：`is_system` → 目标父节点存在 → `new_parent_id != dept_id` → 新父节点的 `path` 不以自身 `path` 为前缀（防止移入后代）→ 更新自身 `path` → 用 `path LIKE '{old_path}%'` 批量更新后代 `path = new_prefix + SUBSTRING(path, LEN(old_path)+1)`。

- [ ] **Step 4: 暴露 `PATCH /departments/{id}/move`**

- [ ] **Step 5: 运行测试至通过**

Run: `uv run pytest tests/test_department_move.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/department.py app/api/departments.py tests/test_department_move.py
git commit -m "feat: add department move with path cascade"
```

---

### Task 7: 菜单领域模型与树 CRUD

**Files:**
- Create: `app/models/menu.py`
- Create: `app/schemas/menu.py`
- Create: `app/repositories/menu.py`
- Create: `app/services/menu.py`
- Create: `app/api/menus.py`
- Modify: `app/main.py`
- Create: `migrations/versions/<rev>_create_menu.py`
- Test: `tests/test_menu_crud.py`

**Interfaces:**
- Consumes: `Base`（Task 2）；`BusinessError`、`ErrorCode`（Task 3）。
- Produces: `Menu` ORM；`MenuType` 枚举（`DIR` / `MENU` / `BUTTON`）；`get_menu_tree(session, include_disabled: bool = False) -> list[MenuNode]`；`list_enabled_permission_codes(session) -> frozenset[str]`（供 `super_admin` 动态权限集使用）。

- [ ] **Step 1: 写 `app/models/menu.py`**

字段：`id`、`parent_id`、`type`（`Enum(MenuType)`）、`name`、`path`（可空）、`component`（可空）、`icon`（可空）、`permission_code`（可空，唯一索引）、`sort_order`、`visible`、`status`。

- [ ] **Step 2: 生成并应用迁移**

Run: `uv run alembic revision --autogenerate -m "create menu" && uv run alembic upgrade head`

- [ ] **Step 3: 写 `app/schemas/menu.py`**

`MenuCreate`、`MenuUpdate`（全可选）、`MenuNode`（含 `children`）。

- [ ] **Step 4: 写测试 `tests/test_menu_crud.py`（先失败）**

```python
async def test_create_dir_then_menu_then_button(): ...
async def test_button_under_button_returns_4001(): ...
async def test_button_under_dir_returns_4001(): ...
async def test_duplicate_permission_code_returns_4002(): ...
async def test_disabled_parent_hides_descendants_from_me_menus(): ...
async def test_list_enabled_permission_codes_excludes_disabled_and_nulls(): ...
async def test_delete_menu_with_children_returns_4001(): ...
```

- [ ] **Step 5: 运行测试确认失败**

Run: `uv run pytest tests/test_menu_crud.py -v`
Expected: FAIL

- [ ] **Step 6: 实现 repository 与 service**

层级校验：`BUTTON` 的父节点必须是 `MENU`；`MENU` 的父节点必须是 `DIR` 或为根；`DIR` 的父节点必须是 `DIR` 或为根。停用节点不参与 `/me/menus`，且其子树整支不返回。

- [ ] **Step 7: 实现 `app/api/menus.py` 并挂载**

`GET /menus/tree`、`POST /menus`、`PATCH /menus/{id}`、`DELETE /menus/{id}`。

- [ ] **Step 8: 运行测试至通过**

Run: `uv run pytest tests/test_menu_crud.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add app/models/menu.py app/schemas/menu.py app/repositories/menu.py app/services/menu.py app/api/menus.py migrations tests/test_menu_crud.py
git commit -m "feat: add menu tree model and CRUD"
```

---

### Task 8: 角色模型、CRUD 与菜单授权

**Files:**
- Create: `app/models/role.py`
- Create: `app/schemas/role.py`
- Create: `app/repositories/role.py`
- Create: `app/services/role.py`
- Create: `app/api/roles.py`
- Modify: `app/main.py`
- Create: `migrations/versions/<rev>_create_role.py`
- Test: `tests/test_role_crud.py`

**Interfaces:**
- Consumes: `Menu`、`list_enabled_permission_codes`（Task 7）。
- Produces: `Role` ORM；`DataScope` 枚举（`ALL` / `DEPT_AND_CHILD` / `DEPT` / `SELF` / `CUSTOM`）；`RoleMenu` 关联表；`get_role(session, role_id) -> Role`；`list_role_permission_codes(session, role: Role) -> frozenset[str]`。

- [ ] **Step 1: 写 `app/models/role.py`**

`Role` 字段：`id`、`code`（唯一）、`name`、`description`、`data_scope`、`data_scope_include_children`（默认 `True`）、`is_system`、`status`。`RoleMenu(role_id, menu_id)` 联合主键。

- [ ] **Step 2: 生成并应用迁移**

Run: `uv run alembic revision --autogenerate -m "create role" && uv run alembic upgrade head`

- [ ] **Step 3: 写 `list_role_permission_codes`**

`super_admin`（`code == "super_admin"` 且 `is_system`）**不查 `role_menu`**，直接返回 `list_enabled_permission_codes(session)`。其余角色按 `role_menu → menu` 取启用菜单的 `permission_code` 并集，忽略 `status=disabled` 与空值。

- [ ] **Step 4: 写测试 `tests/test_role_crud.py`（先失败）**

```python
async def test_create_and_get_role(): ...
async def test_duplicate_role_code_conflicts(): ...
async def test_system_role_cannot_be_deleted(): ...  # 3001
async def test_delete_role_in_use_returns_3002(): ...
async def test_assign_menus_replaces_previous_set(): ...
async def test_disabled_role_yields_empty_permission_codes(): ...
async def test_super_admin_permissions_come_from_all_enabled_menus(): ...
async def test_newly_added_menu_is_automatically_in_super_admin(): ...
```

- [ ] **Step 5: 运行测试确认失败**

Run: `uv run pytest tests/test_role_crud.py -v`
Expected: FAIL

- [ ] **Step 6: 实现 service 与 `app/api/roles.py`**

接口：`GET /roles`、`POST /roles`、`GET /roles/{id}`、`PATCH /roles/{id}`、`DELETE /roles/{id}`、`PUT /roles/{id}/menus`（整树覆盖式）、`PUT /roles/{id}/data-scope`。数据范围为 `CUSTOM` 时校验部门 ID 均存在且非系统"待分配"节点。

- [ ] **Step 7: 运行测试至通过**

Run: `uv run pytest tests/test_role_crud.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/models/role.py app/schemas/role.py app/repositories/role.py app/services/role.py app/api/roles.py migrations tests/test_role_crud.py
git commit -m "feat: add role model, CRUD and menu assignment"
```

---

### Task 9: 用户模型与本地账号 CRUD

**Files:**
- Create: `app/models/user.py`
- Create: `app/models/role_department.py`
- Create: `app/schemas/user.py`
- Create: `app/repositories/user.py`
- Create: `app/services/user.py`
- Create: `app/api/users.py`
- Modify: `app/main.py`
- Create: `migrations/versions/<rev>_create_user.py`
- Test: `tests/test_user_crud.py`

**Interfaces:**
- Consumes: `Department`（Task 5）、`Role`（Task 8）、`BusinessError`（Task 3）。
- Produces: `User` ORM（含 `token_version`）；`RoleDepartment` 关联表；`create_local_user(session, payload) -> User`；`get_user(session, user_id) -> User`。

- [ ] **Step 1: 写 `app/models/user.py` 与 `app/models/role_department.py`**

`User` 字段：`id`、`feishu_open_id`（唯一、可空）、`username`（唯一、可空）、`password_hash`（可空）、`nickname`、`email`、`avatar`、`department_id`（可空）、`role_id`（可空）、`status`、`token_version`（默认 0）、`last_login_at`。`RoleDepartment(role_id, department_id)` 联合主键。

- [ ] **Step 2: 生成并应用迁移**

Run: `uv run alembic revision --autogenerate -m "create user" && uv run alembic upgrade head`

- [ ] **Step 3: 写测试 `tests/test_user_crud.py`（先失败）**

```python
async def test_create_local_user_hashes_password(): ...
async def test_create_local_user_requires_username_and_password(): ...
async def test_duplicate_username_returns_2001(): ...
async def test_create_user_without_role_or_department_is_allowed(): ...
async def test_patch_user_updates_profile_fields(): ...
async def test_password_reset_on_feishu_user_returns_error(): ...
async def test_api_cannot_write_feishu_open_id(): ...
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/test_user_crud.py -v`
Expected: FAIL

- [ ] **Step 5: 实现 service 与 `app/api/users.py`**

`POST /users` 只创建本地账号（`username` + 初始密码），密码用 Argon2 哈希。`feishu_open_id` 不在任何请求 schema 中。`PATCH /users/{id}` 只改资料字段；角色、部门、状态各自独立端点。数据范围过滤在 Task 12 接入。

- [ ] **Step 6: 运行测试至通过**

Run: `uv run pytest tests/test_user_crud.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/models app/schemas/user.py app/repositories/user.py app/services/user.py app/api/users.py migrations tests/test_user_crud.py
git commit -m "feat: add user model and local account CRUD"
```

---

### Task 10: 数据范围过滤函数

**Files:**
- Create: `app/services/data_scope.py`
- Create: `app/core/auth_context.py`
- Modify: `app/services/user.py`
- Modify: `app/api/users.py`
- Test: `tests/test_data_scope.py`

**Interfaces:**
- Consumes: `User`、`Department`、`DataScope`。
- Produces: `AuthContext`（dataclass：`user_id`、`role_id`、`department_id`、`data_scope`、`custom_department_ids`、`include_children`、`permission_codes`、`is_super_admin`）；`build_user_scope_filter(ctx: AuthContext) -> ColumnElement[bool]`；`apply_user_scope(stmt, ctx) -> Select`。

- [ ] **Step 1: 定义 `AuthContext`**

```python
@dataclass(frozen=True)
class AuthContext:
    user_id: int
    role_id: int | None
    department_id: int | None
    data_scope: DataScope | None
    custom_department_ids: frozenset[int]
    include_children: bool
    permission_codes: frozenset[str]
    is_super_admin: bool
```

- [ ] **Step 2: 写测试 `tests/test_data_scope.py`（先失败）**

固定树：`总公司(1) → 研发部(2) → 前端组(3)`，`总公司(1) → 市场部(4)`；用户 `alice@研发部`、`bob@研发部`、`carol@前端组`、`dave@市场部`；观察者放在研发部。

```python
@pytest.mark.parametrize("scope,expected", [
    (DataScope.ALL, {"alice", "bob", "carol", "dave"}),
    (DataScope.DEPT, {"alice", "bob"}),
    (DataScope.DEPT_AND_CHILD, {"alice", "bob", "carol"}),
    (DataScope.SELF, {"alice"}),
])
async def test_user_list_respects_data_scope(scope, expected): ...

async def test_custom_scope_without_children_matches_exact_departments(): ...
async def test_custom_scope_with_children_includes_subtree(): ...
async def test_user_with_null_department_sees_nothing_for_dept_scopes(): ...
async def test_user_with_null_department_still_sees_self(): ...
async def test_pending_assignment_department_is_not_in_any_scope(): ...
async def test_user_with_null_role_falls_back_to_self_only(): ...
async def test_detail_endpoint_is_scoped_too(): ...
async def test_write_on_invisible_user_returns_403(): ...
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_data_scope.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 `build_user_scope_filter`**

按 spec 7.3 表格实现五档分支。`DEPT_AND_CHILD` / `CUSTOM+children` 用 `Department.path` 前缀匹配子查询，不递归。`SELF` 用 `User.id == ctx.user_id`（本期用户资源无独立 owner 字段，本人即 owner）。空部门/空角色的退化行为见 spec 7.3 边界规则。

- [ ] **Step 5: 接入 `GET /users`、`GET /users/{id}` 及用户写端点**

列表与详情套 `apply_user_scope`；写端点先查可见性，不可见返回 `403`。

- [ ] **Step 6: 运行测试至通过**

Run: `uv run pytest tests/test_data_scope.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/core/auth_context.py app/services/data_scope.py app/services/user.py app/api/users.py tests/test_data_scope.py
git commit -m "feat: add department-scoped data permission filter"
```

---

### Task 11: 认证基础设施与本地登录

**Files:**
- Create: `app/core/security.py`
- Create: `app/core/deps.py`
- Create: `app/services/auth.py`
- Create: `app/api/auth.py`
- Modify: `app/main.py`
- Test: `tests/test_local_auth.py`

**Interfaces:**
- Consumes: `settings`、`redis_client`、`User`、`AuthContext`。
- Produces: `create_access_token(user_id: int, token_version: int) -> str`；`decode_access_token(token: str) -> dict`；`hash_password(plain: str) -> str`；`verify_password(plain: str, hashed: str) -> bool`；`get_auth_snapshot(session, redis, user_id) -> AuthSnapshot`；`invalidate_auth_snapshot(redis, user_id) -> None`；`get_current_context(...) -> AuthContext`（FastAPI 依赖）；`bump_token_version(session, user_id) -> None`。

- [ ] **Step 1: 写测试 `tests/test_local_auth.py`（先失败）**

```python
async def test_local_login_returns_tokens_and_profile(): ...
async def test_local_login_with_wrong_password_returns_1001(): ...
async def test_disabled_user_cannot_login(): ...  # 1002
async def test_access_token_carries_token_version(): ...
async def test_refresh_rotates_and_old_refresh_is_rejected(): ...
async def test_logout_invalidates_refresh_token(): ...
async def test_logout_all_removes_every_device(): ...
async def test_access_token_rejected_after_token_version_bump(): ...
async def test_auth_snapshot_cache_is_populated_and_invalidated(): ...
async def test_login_rate_limit_returns_1005_after_threshold(): ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_local_auth.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `app/core/security.py`**

Argon2 哈希/校验；JWT 载荷含 `sub`、`ver`、`jti`、`exp`。

- [ ] **Step 4: 实现认证快照与 `get_current_context`**

`AuthSnapshot` 缓存 `status`、`token_version`、`role_id`、`department_id`，键为 `{app}:{env}:auth:snapshot:{user_id}`，TTL 10 分钟。`get_current_context` 解析 Bearer token → 比对快照 `token_version` 与 `status` → 组装 `AuthContext`（含角色权限码）。任一不符返回 `1003`/`1004`。

- [ ] **Step 5: 实现 `app/api/auth.py`**

`POST /auth/login`、`POST /auth/refresh`、`POST /auth/logout`、`POST /auth/logout-all`、`GET /auth/me`。登录限流用 Redis 计数，IP 与账号双维度。

- [ ] **Step 6: 运行测试至通过**

Run: `uv run pytest tests/test_local_auth.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/core/security.py app/core/deps.py app/services/auth.py app/api/auth.py tests/test_local_auth.py
git commit -m "feat: add JWT auth, local login and session invalidation"
```

---

### Task 12: 权限依赖与权限缓存

**Files:**
- Create: `app/core/permissions.py`
- Modify: `app/core/deps.py`
- Modify: `app/api/departments.py`, `app/api/menus.py`, `app/api/roles.py`, `app/api/users.py`
- Test: `tests/test_permission_dependency.py`

**Interfaces:**
- Consumes: `AuthContext`、`list_role_permission_codes`（Task 8）。
- Produces: `require_permission(code: str) -> Callable[..., Awaitable[AuthContext]]`；`get_role_permission_codes(session, redis, role) -> frozenset[str]`（`list_role_permission_codes` 的带缓存包装，角色维度缓存）；`invalidate_role_permissions(redis, role_id: int) -> None`。

- [ ] **Step 1: 写测试 `tests/test_permission_dependency.py`（先失败）**

```python
async def test_endpoint_requires_declared_permission(): ...  # 403
async def test_endpoint_passes_with_permission(): ...  # 200
async def test_permission_codes_are_cached_in_redis(): ...
async def test_role_menu_change_invalidates_permission_cache(): ...
async def test_menu_status_change_invalidates_super_admin_cache(): ...
async def test_me_permissions_returns_current_user_codes(): ...
async def test_me_menus_returns_only_visible_tree(): ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_permission_dependency.py -v`
Expected: FAIL

- [ ] **Step 3: 实现权限缓存**

键 `{app}:{env}:perm:role:{role_id}`，TTL 10 分钟。`super_admin` 用特殊键 `{app}:{env}:perm:super_admin`，任何菜单写操作后失效。

- [ ] **Step 4: 实现 `require_permission` 并替换各路由占位依赖**

按 spec 9 节给每条路由挂上对应权限码。

- [ ] **Step 5: 实现 `GET /me/menus` 与 `GET /me/permissions`**

`/me/menus` 返回当前用户角色的目录/菜单树（不含按钮，按钮走权限码）；`super_admin` 返回全部启用菜单。

- [ ] **Step 6: 运行测试至通过**

Run: `uv run pytest tests/test_permission_dependency.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/core/permissions.py app/core/deps.py app/api tests/test_permission_dependency.py
git commit -m "feat: add permission dependency and role permission cache"
```

---

### Task 13: 防提权与授权上限规则

**Files:**
- Create: `app/services/authorization.py`
- Modify: `app/services/user.py`, `app/api/users.py`
- Modify: `app/services/role.py`, `app/api/roles.py`
- Modify: `app/services/auth.py`
- Test: `tests/test_privilege_escalation.py`

**Interfaces:**
- Consumes: `AuthContext`、`BusinessError`、`bump_token_version`。
- Produces: `assignable_role_ids(ctx, all_roles) -> set[int]`；`assert_can_assign_role(ctx, target_role) -> None`；`assert_target_visible(ctx, target_user) -> None`；`assert_not_self(ctx, target_user) -> None`；`assert_super_admin_remains(session, operation) -> None`。

- [ ] **Step 1: 写测试 `tests/test_privilege_escalation.py`（先失败）**

```python
async def test_non_super_admin_cannot_grant_super_admin(): ...  # 2003
async def test_super_admin_can_grant_super_admin(): ...
async def test_anyone_cannot_change_own_role(): ...  # 2002
async def test_anyone_cannot_change_own_department(): ...
async def test_anyone_cannot_change_own_status(): ...
async def test_cannot_disable_last_super_admin(): ...  # 2004
async def test_cannot_demote_last_super_admin(): ...
async def test_cannot_delete_local_emergency_superadmin(): ...
async def test_assigning_user_to_disabled_department_is_rejected(): ...
async def test_forbidden_assignment_returns_403_not_silent_trim(): ...
async def test_role_change_bumps_token_version_and_clears_sessions(): ...
async def test_department_change_bumps_token_version(): ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_privilege_escalation.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `app/services/authorization.py`**

按 spec 8.5 实现五条规则。规则 4 要求"越权即拒绝"：请求体中的角色/部门先整体校验，非法则抛 `403`，绝不静默裁剪。

- [ ] **Step 4: 接入用户与角色写端点**

`PATCH /users/{id}/role`、`PATCH /users/{id}/department`、`PATCH /users/{id}/status`、`DELETE /users/{id}`、`DELETE /roles/{id}`、`PATCH /roles/{id}` 全部前置校验。

- [ ] **Step 5: 变更后立即失效**

角色或部门变更后调用 `bump_token_version` + `invalidate_auth_snapshot` + 清除全部 refresh token；角色权限变更额外 `invalidate_role_permissions`。

- [ ] **Step 6: 运行测试至通过**

Run: `uv run pytest tests/test_privilege_escalation.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/authorization.py app/services/user.py app/services/role.py app/services/auth.py app/api tests/test_privilege_escalation.py
git commit -m "feat: enforce authorization limits and immediate session invalidation"
```

---

### Task 14: 飞书 OAuth 登录与自动建号

**Files:**
- Create: `app/clients/__init__.py`, `app/clients/feishu.py`
- Modify: `app/services/auth.py`
- Modify: `app/api/auth.py`
- Test: `tests/test_feishu_login.py`

**Interfaces:**
- Consumes: `get_auth_snapshot`、`create_access_token`（Task 11）。
- Produces: `FeishuClient.exchange_code(code: str) -> str`；`FeishuClient.get_user_info(user_access_token: str) -> FeishuUserInfo`；`FeishuUserInfo(open_id, name, avatar_url, email)`；`login_with_feishu(session, redis, code: str) -> tuple[str, str, User]`。

- [ ] **Step 1: 核对飞书官方文档**

实现前查飞书开放平台当前文档，确认换取 `user_access_token` 与获取用户信息的接口路径、请求方式、响应字段与错误码。不凭记忆实现。

- [ ] **Step 2: 写测试 `tests/test_feishu_login.py`（先失败）**

用 `respx` 或 `httpx.MockTransport` 打桩飞书接口。

```python
async def test_first_login_auto_provisions_user(): ...
async def test_new_user_lands_in_pending_department_with_default_role(): ...
async def test_existing_user_reuses_record_by_open_id(): ...
async def test_login_backfills_only_empty_profile_fields(): ...
async def test_admin_edited_nickname_is_not_overwritten(): ...
async def test_avatar_and_last_login_always_refreshed(): ...
async def test_disabled_user_login_returns_1002(): ...
async def test_feishu_error_response_maps_to_business_error(): ...
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_feishu_login.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 `app/clients/feishu.py`**

用 `httpx.AsyncClient` 调用飞书接口，超时与错误码映射为 `BusinessError`。

- [ ] **Step 5: 实现 `login_with_feishu` 与 `POST /auth/feishu/login`**

按 spec 8.1 流程：换 token → 取用户信息 → 按 `feishu_open_id` 查用户 → 建号或补资料 → 签发 token。新用户 `department_id = 待分配节点 id`、`role_id = default 角色 id`。

- [ ] **Step 6: 运行测试至通过**

Run: `uv run pytest tests/test_feishu_login.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/clients app/services/auth.py app/api/auth.py tests/test_feishu_login.py
git commit -m "feat: add Feishu OAuth login with auto provisioning"
```

---

### Task 15: 种子数据与初始化脚本

**Files:**
- Create: `scripts/__init__.py`, `scripts/init_data.py`
- Create: `tests/test_seed_data.py`

**Interfaces:**
- Consumes: 全部模型与 service。
- Produces: `init_data(session, *, superadmin_username: str, superadmin_password: str) -> None`（幂等）。

- [ ] **Step 1: 写测试 `tests/test_seed_data.py`（先失败）**

```python
async def test_init_creates_pending_department_as_system_root(): ...
async def test_init_creates_super_admin_and_default_roles(): ...
async def test_default_role_is_scoped_to_self(): ...
async def test_init_creates_baseline_menu_tree(): ...
async def test_init_creates_local_emergency_superadmin(): ...
async def test_init_is_idempotent_when_run_twice(): ...
async def test_init_requires_superadmin_password_from_env(): ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_seed_data.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 `scripts/init_data.py`**

幂等：按 `is_system` 与固定 `code` 查找，存在则跳过。超管密码从 `settings.initial_superadmin_password` 读取，为空则报错退出，不得回退到硬编码默认值。

- [ ] **Step 4: 运行测试至通过**

Run: `uv run pytest tests/test_seed_data.py -v`
Expected: PASS

- [ ] **Step 5: 在本地库执行一次**

Run: `uv run python -m scripts.init_data`
Expected: 创建待分配部门、两个系统角色、基础菜单、一个本地超管

- [ ] **Step 6: Commit**

```bash
git add scripts tests/test_seed_data.py
git commit -m "feat: add idempotent seed data initialization"
```

---

### Task 16: 端到端验收与文档收口

**Files:**
- Create: `tests/test_e2e_permission_flow.py`
- Create: `README.md`
- Modify: `docs/superpowers/specs/2026-10-16-user-permission-backend-design.md`（仅在实现与设计有偏差时更新）

**Interfaces:**
- Consumes: 全部。
- Produces: 可运行的完整系统与操作说明。

- [ ] **Step 1: 写端到端测试 `tests/test_e2e_permission_flow.py`**

```python
async def test_full_flow_from_seed_to_scoped_admin_operations():
    """超管登录 → 建部门树 → 建角色并授权 → 建用户并分配 → 用户登录 →
    验证 /auth/me、/me/menus、/me/permissions 与受限的 /users 列表。"""
```

- [ ] **Step 2: 运行全量测试**

Run: `uv run pytest -v`
Expected: 全部 PASS

- [ ] **Step 3: 运行静态检查与类型检查**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: 无错误

- [ ] **Step 4: 从空库验证迁移与种子全链路**

Run: `uv run alembic downgrade base && uv run alembic upgrade head && uv run python -m scripts.init_data`
Expected: 无错误，且可重复执行升级

- [ ] **Step 5: 写 `README.md`**

包含：环境要求、`.env` 配置说明、启动 MySQL/Redis、迁移与种子命令、启动服务命令、测试命令、接口文档地址。

- [ ] **Step 6: Commit**

```bash
git add tests/test_e2e_permission_flow.py README.md docs
git commit -m "test: add end-to-end permission flow and project documentation"
```

---

## 附：任务依赖顺序

```
Task 1 → Task 2 → Task 3 → Task 4
                    ↓
Task 5 → Task 6
Task 7 → Task 8 → Task 9 → Task 10 → Task 11 → Task 12 → Task 13 → Task 14
                                                              ↘
Task 15（依赖 5、7、8、9）→ Task 16（依赖全部）
```

Task 5–14 之间只有单向依赖，可顺序执行；Task 15 可在 Task 9 完成后并行准备，但需在 Task 16 前完成。
