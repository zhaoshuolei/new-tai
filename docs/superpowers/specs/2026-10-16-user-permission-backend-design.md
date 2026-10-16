# 用户与权限管理后端设计

日期：2026-10-16
状态：待确认

## 1. 意图与背景

团队需要一个内部管理后台的后端，承载用户、角色、菜单、部门四块能力。规模约 30 人，单租户，不做对外交付，因此优先选择"结构简单、排查容易"的方案，而不是通用平台的灵活性。

身份认证复用飞书：飞书只回答"这个人是谁"，不参与组织结构和权限。部门树与权限数据全部由本系统自管理。

## 2. 目标

- 用户生命周期管理：飞书首次登录自动建号，管理员维护资料、部门、角色、启停。
- 角色管理：角色的功能权限（菜单/按钮）与数据权限（部门范围）配置。
- 菜单管理：一棵树同时服务前端动态路由与按钮级权限控制。
- 部门管理：可维护的树形结构，作为数据权限的层级基础。
- 后端在接口层强制校验权限与数据范围，前端隐藏按钮仅作为体验优化。

## 3. 非目标

- 操作审计日志（下期）。
- 飞书通讯录/部门同步（部门由后台自维护）。
- 多租户、对外开放 API、OAuth 授权服务。
- 用户自助注册、邮箱/短信找回密码。
- 前端实现（本设计只定义后端契约）。
- 部门树拖拽排序等富交互（接口预留，界面不在范围）。

## 4. 反目标

- 不做"为了通用而通用"的 RBAC：不引入用户多角色并集、权限组继承、条件权限表达式。
- 不把飞书组织信息当作事实来源，避免两套组织结构互相污染。
- 不把"按钮隐藏"当作安全边界。任何写操作必须在后端重新校验。
- 不为 30 人规模引入微服务、消息队列、分布式缓存集群。

## 5. 约束

- 环境：WSL，MySQL 与 Redis 均为本机已运行服务。
- 语言/框架：Python + FastAPI。
- 不使用 PostgreSQL（早期讨论曾提及，已改为 MySQL）。
- 单实例部署，会话与限流状态放在 Redis，为将来多实例留出路。
- 代码注释与文档使用中文；代码标识符、表名、字段名使用英文。

## 6. 技术选型（本次决定的 how）

以下为本设计的选择，若实现者发现冲突需回到设计确认：

- Web 框架：FastAPI。
- ORM：SQLAlchemy 2.0 async 风格，驱动 `asyncmy`。
- 迁移：Alembic，所有结构变更走迁移脚本，禁止手工改表。
- 数据库：MySQL 8。
- 会话/限流：Redis，用于 refresh token 存储、登录限流与登出吊销；授权数据不缓存，以数据库为准。
- 密码哈希：Argon2（仅用于本地应急账号）。
- 配置：`pydantic-settings`，密钥经环境变量注入，不落库不入库。
- 本地编排：Docker Compose 可选；当前 WSL 已直装 MySQL/Redis，优先直连本机服务。

Python 版本注记：当前机器为 Python 3.14。若依赖链尚不完整，允许降级至 3.12 并在 `pyproject.toml` 固定版本。此为构建者裁量。

## 7. 领域模型

### 7.1 关系约束（已决定）

- 用户归属**单个部门**（`user.department_id`）。
- 用户拥有**单个角色**（`user.role_id`）。
- 因此无需 `user_role`、`user_department` 关联表，权限来源唯一，排查直观。
- 角色与菜单多对多；角色与"自定义数据范围部门"多对多。

### 7.2 表结构

`department`
- `id`, `parent_id`（可空，根为 NULL）, `name`
- `path`：物化路径，形如 `/1/3/7/`，用于子树查询
- `sort_order`, `status`, `created_at`, `updated_at`
- 约束：`parent_id` 不可指向自身或后代（移动时校验）。

`user`
- `id`
- `feishu_open_id`：唯一，可空（空值表示本地应急账号）
- `username`：唯一，可空（本地账号使用）
- `password_hash`：可空（仅本地应急账号）
- `nickname`, `email`, `avatar`
- `department_id`：可空表示"待分配"
- `role_id`：可空表示尚未授权
- `status`：`active` / `disabled`
- `token_version`：整数，默认 0；递增即令该用户已签发的所有 access token 失效
- `last_login_at`, `created_at`, `updated_at`

`role`
- `id`, `code`（唯一）, `name`, `description`
- `data_scope`：枚举 `ALL` / `DEPT_AND_CHILD` / `DEPT` / `SELF` / `CUSTOM`
- `data_scope_include_children`：布尔，仅对 `CUSTOM` 生效，默认 true
- `is_system`：布尔，内置角色不可删除
- `status`, `created_at`, `updated_at`

`role_department`（仅 `CUSTOM` 使用）
- `role_id`, `department_id`，联合主键

`menu`
- `id`, `parent_id`
- `type`：枚举 `DIR`（目录）/ `MENU`（菜单）/ `BUTTON`（按钮）
- `name`（显示名）, `path`（前端路由）, `component`（前端组件路径）
- `icon`, `permission_code`（如 `user:create`）, `sort_order`
- `visible`（是否在侧边栏显示）, `status`
- 约束：`BUTTON` 必须挂在 `MENU` 下；`permission_code` 在全局唯一（非空时）。

`role_menu`
- `role_id`, `menu_id`，联合主键

### 7.3 数据权限语义（唯一权威定义）

给定当前用户与目标查询的部门字段：

| data_scope | 过滤条件 |
| --- | --- |
| `ALL` | 不追加条件 |
| `DEPT_AND_CHILD` | 目标部门 ∈ 当前用户部门的子树（含自身） |
| `DEPT` | 目标部门 = 当前用户部门 |
| `SELF` | 仅本人创建/归属的记录（按业务表上的 owner 字段） |
| `CUSTOM` | 目标部门 ∈ 自定义部门集合；`include_children=true` 时取这些部门的子树并集，否则仅这些部门本身 |

边界规则：
- 用户 `department_id` 为空（待分配）时，`DEPT*` 与 `CUSTOM` 均退化为"无可见数据"，但 `SELF` 仍可见本人记录。
- 用户 `role_id` 为空时，功能权限为空，数据权限按最小化处理（仅本人）。
- 子树查询统一走 `department.path` 前缀匹配，不递归查询数据库。

### 7.4 数据权限的作用范围（修订：2026-10-16 客户管理设计 D3）

数据范围作用于**业务数据资源**：`user`（用户）与 `customer`（客户，见《客户管理与天眼查企业信息回填设计》）。

- 用户：`GET /users` 列表与 `GET /users/{id}` 详情受数据范围约束；对用户的写操作先做同样的可见性校验，不可见的用户不可修改、不可改派。部门列为 `user.department_id`，SELF 档位以用户自身 `id` 为准。
- 客户：列表、详情、更新、删除、负责人转移均受数据范围约束。部门列为 `customer.owner_department_id`，SELF 档位以 `customer.owner_user_id` 为准。

角色、菜单、部门属于**配置资源**，只受功能权限（`role:*` / `menu:*` / `dept:*`）约束，不受数据范围约束。

过滤器必须实现为统一的、可复用的函数，接受"部门列"与"所有者列"作为参数，用户与客户共用，禁止各接口各写一套。用户资源的过滤结果在修订前后必须保持一致，由既有数据范围测试兜底。

### 7.5 状态字段语义（本次决定）

- 用户 `disabled`：拒绝登录；已签发 token 立即失效（见 8.2）；保留记录以供数据引用。
- 角色 `disabled`：该角色用户功能权限为空，数据范围退化为 `SELF`；角色记录本身仍可被用户引用。
- 菜单/目录 `disabled`：不参与权限码计算，也不出现在 `/me/menus` 中；目录停用时连带隐藏其全部后代路由。
- 部门 `disabled`：仍参与历史数据过滤与用户归属，但不可用于新分配（部门选择器不返回）。

### 7.6 "待分配"节点的定位（本次决定）

"待分配"是**独立的系统根节点**，与业务部门根节点平级，不是业务部门的祖先。这样可以避免 `DEPT_AND_CHILD` 从待分配节点向上覆盖整个组织。

它 `is_system=true`：不可改名、不可移动、不可删除，且**不参与数据范围选择器的可选列表**（不能作为 `CUSTOM` 目标，也不参与档位计算）。新用户默认落在该节点，在管理员手工调整前看不到任何部门数据。

## 8. 认证与授权链路

### 8.1 飞书登录

1. 前端跳转飞书授权页，拿到 `code`。
2. 前端调用 `POST /auth/feishu/login`，提交 `code`。
3. 后端用 `code` 换 `user_access_token`，再拉取用户信息（`open_id`、姓名、头像、邮箱）。
4. 按 `feishu_open_id` 查用户：
   - 已存在：更新昵称/头像/邮箱，刷新 `last_login_at`；`status=disabled` 时拒绝登录。
   - 不存在：自动建号，`department_id = 待分配部门`，`role_id = 默认角色`。
5. 签发自有 JWT，返回 `access_token` + `refresh_token`。

默认角色与待分配部门由种子数据创建（见 10.2）。选择理由：授权链闭合、无需管理员手工初始化即可登录查看；权限范围最小。

### 8.2 Token 策略

- `access_token`：JWT，有效期 30 分钟，含 `sub`(user_id)、`ver`(token_version)、`jti`、`exp`。
- `refresh_token`：随机不透明串，存 Redis，有效期 7 天，键含用户与设备标识。
- `POST /auth/refresh`：校验并**轮换** refresh token（旧的立即失效）。
- `POST /auth/logout`：删除当前设备的 refresh token。
- `POST /auth/logout-all`：删除该用户全部 refresh token。

即时失效（本次决定）：JWT 不是纯无状态。每个认证请求按主键回源数据库读取用户行，比对 `status` 与 token 中的 `token_version`，并从用户行实时组装授权上下文；不使用任何 Redis 授权快照或权限码缓存。用户状态、角色、部门等安全变更与 `token_version` 递增在同一个数据库事务内提交；旧 access token 的即时失效由每次请求的数据库 `token_version` 比对权威保证，不依赖任何 Redis 操作。事务提交后再尽力清理该用户全部 refresh token（失败仅记日志，不回滚已提交的安全变更），不再等待 30 分钟自然过期。Redis 只承载 refresh token、登录限流与登出簿记。30 人规模下这次按主键查询成本可忽略。

### 8.3 本地应急账号

保留一个本地超级管理员（`username` + `password_hash`，`feishu_open_id` 为空），通过 `POST /auth/login` 用户名密码登录。用途：飞书不可用或超管飞书账号被回收时仍能进入系统。该账号受与其他用户相同的接口校验，不做特殊豁免。

### 8.4 功能权限校验

- FastAPI 依赖：`require_permission("user:create")`。
- 校验来源：当前用户 → 角色 → `role_menu` → `menu.permission_code` 集合，每次请求从数据库实时计算。
- 不缓存该集合（也不缓存用户认证快照）；角色或菜单变更后下一次请求即生效。
- 权限码命名为 `资源:动作`，动作固定为 `create` / `read` / `update` / `delete` / `assign` 等有限集合。

### 8.5 授权上限与防提权（本次决定）

功能权限只能回答"能不能执行这个操作"，不能回答"能操作到什么程度"。例如只有 `user:assign` 而没有额外约束时，任何管理员都能把别人或自己改成 `super_admin`。因此增设以下授权规则，任何用户管理接口都必须先过这道校验：

1. **`super_admin` 只能由 `super_admin` 授予或撤销。** 非超管持有 `user:assign` 时，其可分配角色集合为"除 `super_admin` 外的全部启用角色"。
2. **任何用户不得修改自己的角色、部门或状态。** 包括超管本人。避免自我提权与把自己锁死在系统外。
3. **系统内必须至少保留一个可用 `super_admin`。** 任何会导致超管数量归零的操作（停用、降级、删除）一律拒绝。本地应急超管（10.4）必须始终保留，是飞书不可用时的最终退路。
4. **越权即拒绝，不静默裁剪。** 请求体中出现当前用户无权授予的角色或部门时返回 `403`，而不是忽略该字段——静默裁剪会让调用方以为授权已生效。
5. 以上规则在 service 层集中实现为可复用的"可授予集合"判断，不散落在各接口。

理由：这套规则用最小代价覆盖自我提权、横向越权、系统失管三类风险，且没有引入角色等级、权限继承等为 30 人规模不必要的复杂度。

## 9. 接口契约（设计级）

统一前缀 `/api/v1`。统一响应包装与错误码约定由构建者确定，但需全局一致。

认证
- `POST /auth/feishu/login`
- `POST /auth/login`（本地应急）
- `POST /auth/refresh`
- `POST /auth/logout`
- `GET /auth/me`：当前用户资料、部门、角色、数据范围

用户管理（需 `user:*`）
- `GET /users`：分页 + 按部门/角色/状态/关键字筛选，**受当前用户数据范围约束**
- `POST /users`、`GET /users/{id}`、`PATCH /users/{id}`
- `PATCH /users/{id}/department`、`PATCH /users/{id}/role`
- `PATCH /users/{id}/status`
- `POST /users/{id}/password/reset`（仅本地账号有意义）

角色管理（需 `role:*`）
- `GET /roles`、`POST /roles`、`GET /roles/{id}`、`PATCH /roles/{id}`、`DELETE /roles/{id}`
- `PUT /roles/{id}/menus`：整树覆盖式授权
- `PUT /roles/{id}/data-scope`：设置 `data_scope`、`include_children`、自定义部门集合

菜单管理（需 `menu:*`）
- `GET /menus/tree`：管理用完整树
- `POST /menus`、`PATCH /menus/{id}`、`DELETE /menus/{id}`
- `GET /me/menus`：当前用户可见的目录/菜单树（供前端渲染路由）
- `GET /me/permissions`：当前用户权限码列表（供前端按钮显隐）

部门管理（需 `dept:*`）
- `GET /departments/tree`、`POST /departments`、`PATCH /departments/{id}`、`DELETE /departments/{id}`
- 删除前校验：存在子部门或存在用户时拒绝，返回明确原因。

## 10. 种子数据

10.1 `department`：根节点"待分配"（系统保留，不可删除、不可移动）。

10.2 `role`：
- `super_admin`：`data_scope=ALL`，`is_system=true`，不可删除、不可改名、不可停用。其权限集**不落库**：计算时动态等于"全部启用菜单的权限码"，因此新增菜单自动获得，不会出现"新菜单无人可授权"。`role_menu` 中不为它保存记录。
- `default`：`data_scope=SELF`，仅基础菜单（个人资料页）；新用户默认分配。

10.3 `menu`：按上述两个角色需要的最小集合初始化，作为后台配置界面的起点。`super_admin` 无需挂载，`default` 需显式挂载。

10.4 `user`：一个本地应急超管（`feishu_open_id` 为空），初始密码由环境变量或一次性初始化脚本设置，**不写入代码或仓库**。该账号在系统内必须始终保留至少一个：任何接口不得删除它、停用它、或移除其 `super_admin` 角色。

10.5 资料同步规则（本次决定）：飞书登录时只**补充空字段**并刷新头像与 `last_login_at`；管理员已编辑过的昵称、邮箱不被打回。若需以飞书资料覆盖，属后续功能。

10.6 `POST /users` 的语义（本次决定）：只创建**本地账号**（`username` + 初始密码），用于应急或服务用途。飞书用户无法预创建，只能由首次登录自动生成；`feishu_open_id` 不接受 API 写入，也不可手工改绑。

## 11. 影响面与风险

- 数据范围是横切关注点，会渗透到每个列表/详情接口。构建者应抽出统一的过滤条件生成函数，禁止各接口各写一套。
- `department.path` 必须在部门移动时级联更新所有后代；这是最容易出错的地方，需有专门测试。
- 用户角色/部门等安全变更必须在同一个数据库事务内递增 `token_version`；授权完全以数据库为准，无缓存可失效，否则权限变更不即时生效属于安全缺陷。refresh token 的清理在事务提交之后执行且为尽力而为（失败仅记日志）；旧 access token 的即时失效由每次请求的数据库 `token_version` 比对保证，而非由 Redis 清理保证。
- 飞书 API 的字段与错误码需以官方文档为准，构建时核对，不凭记忆实现。
- Python 3.14 的依赖可用性需实测。

## 12. 基础底座设计

本节覆盖"业务功能之外、但决定项目能否长期维护"的通用工程实践。它们是本设计的默认基线，除非实现时发现与某条业务约束冲突。

### 12.1 分层与目录

```
app/
  api/          # FastAPI 路由（只做参数解析与响应组装）
  schemas/      # Pydantic 请求/响应模型
  services/     # 业务逻辑、事务边界、授权规则
  repositories/ # 数据访问，不含业务判断
  models/       # SQLAlchemy ORM
  core/         # 配置、安全、依赖、异常、日志、响应
  db/           # 引擎、会话、Base
migrations/     # Alembic
scripts/        # 初始化与运维脚本
tests/
```

依赖方向单向：`api → services → repositories → models`。禁止反向引用，禁止在路由里直接写 SQL。

### 12.2 统一响应与错误码

- 成功：`{"code": 0, "message": "ok", "data": ...}`；分页数据统一为 `{"items": [], "total": 0, "page": 1, "page_size": 20}`。
- 失败：`{"code": <业务码>, "message": "...", "request_id": "..."}`，生产环境不返回堆栈。
- 异常体系：业务异常基类 `BusinessError(code, message, http_status)`；全局 handler 覆盖业务异常、`RequestValidationError`、`HTTPException` 与未捕获异常。
- 业务码分段：`1xxx` 通用/认证、`2xxx` 用户、`3xxx` 角色、`4xxx` 菜单、`5xxx` 部门。
- HTTP 状态码同时正确使用：`401` 未认证、`403` 无权限、`404` 不存在、`409` 冲突、`422` 参数错误。

### 12.3 请求追踪与日志

- `request_id` 中间件：注入日志上下文，并回写响应头 `X-Request-Id`。
- 结构化日志：本地可读格式、生产 JSON 格式，由配置切换。
- 访问日志字段：方法、路径、状态码、耗时、`user_id`。
- 敏感字段脱敏：`password`、`token`、`code`、`authorization` 一律不落日志。

### 12.4 数据库与事务

- 每请求一个 async session，用 FastAPI 依赖 `yield` 管理生命周期。
- 事务边界在 service 层：一个用例一个事务，repository 只做查询与持久化，不自行 `commit`。
- 连接池显式配置：`pool_size`、`max_overflow`、`pool_pre_ping`、`pool_recycle`，不要用默认值上线。
- 统一 ORM Base 提供 `id`、`created_at`、`updated_at`；由业务修改的配置表再加 `updated_by`。
- 删除策略：本期不做全局软删除（会让每条查询都带上过滤条件，容易漏）。用户/角色/菜单/部门一律**前置校验后硬删除**；需要保留历史的用 `status` 停用。
- 字符集 `utf8mb4`；表与列统一 snake_case。
- 时区：数据库统一存 UTC（`DATETIME(6)`），API 返回 ISO8601 带时区，前端负责本地化展示。
- 索引：所有外键列建索引；`menu.permission_code` 唯一索引；`department.path` 建前缀索引供子树查询。

### 12.5 Redis 使用

- Redis 封装为单一 async 客户端，键名统一 `{app}:{env}:{domain}:{ident}`。
- 授权为数据库权威：每次认证请求回源用户行并实时计算权限码集合，不缓存用户认证快照，也不缓存角色权限码集合；因此不存在需要显式失效的授权缓存。
- Redis 只保存 refresh token、登录限流计数与登出簿记；这些键均设 TTL，写入与清理集中在 service 层。
- 安全变更（状态、角色、部门、密码）在同一个数据库事务内递增 `token_version`；事务提交后尽力清理 refresh token（失败仅记日志，不回滚安全变更）。旧 access token 的即时失效由每次请求的数据库 `token_version` 比对权威保证，不依赖 Redis 清理。

### 12.6 安全基线

- CORS 白名单由配置注入，不使用 `*` 配合凭证。
- 登录接口限流：基于 Redis 对 IP 与账号双维度计数，连续失败达阈值锁定一段时间。
- 本地账号密码策略：最小长度与复杂度校验，Argon2 哈希。
- JWT 密钥从环境变量注入，禁止硬编码；提供密钥轮换路径。
- 安全响应头（`X-Content-Type-Options`、`X-Frame-Options` 等）统一由中间件添加。
- 权限校验一律在后端完成，前端隐藏按钮不作为安全边界（与第 4 节反目标一致）。

### 12.7 可观测与运维

- 健康检查：`/health/live` 只探测进程，`/health/ready` 探测 MySQL 与 Redis。
- 启动时 fail-fast 校验关键配置，缺失即拒绝启动。
- 优雅停机，等待在途请求结束并释放连接池。
- OpenAPI 文档按 tag 分组，作为接口契约的唯一事实来源。

### 12.8 工程化与测试

- 依赖管理：`pyproject.toml` + `uv`（或 `poetry`），锁文件入库。
- 代码质量：`ruff`（lint + format），类型检查 `mypy`，配置 `pre-commit`。
- 测试：`pytest` + `pytest-asyncio` + `httpx.AsyncClient`；测试用独立数据库，用例级事务回滚保证隔离。
- 必测项（与第 11 节风险对应）：数据范围各档位过滤、部门树 `path` 移动级联、防提权规则、授权变更即时生效（`token_version` 与数据库权威读取）。
- `.env.example` 入库，真实 `.env` 与任何密钥不入库。

### 12.9 本期明确不做

- 国际化（i18n）。
- 请求幂等键与重放保护（内部后台、无支付类场景）。
- 全局软删除与回收站。
- 消息队列、异步任务队列、微服务拆分。
- 定时任务：过期数据靠 Redis TTL 自动回收，无需调度器。

## 13. 留给构建者的决定

- 项目目录结构与分层（router / service / repository 的取舍）。
- 统一响应体、错误码、分页参数的具体形状。
- Redis 键命名与过期策略细节。
- 测试策略与覆盖率目标（建议：数据范围过滤与部门树移动必须有测试）。
- 后台管理员配置界面的接口分页/排序细节。
- 日志方案（应用日志不在"审计日志"范围内，正常保留）。

## 14. 已确认的决策记录

| 决策 | 结论 |
| --- | --- |
| 部署场景 | 单租户内部后台，约 30 人 |
| 语言框架 | Python + FastAPI |
| 身份认证 | 飞书 OAuth，另保留本地应急超管 |
| 部门来源 | 后台自维护，不与飞书同步 |
| 新用户归属 | 自动建号 → 待分配部门 → 管理员手工调整 |
| 用户-部门 | 单部门 |
| 用户-角色 | 单角色 |
| 菜单模型 | 单棵树，`type` 区分目录/菜单/按钮 |
| 数据范围 | 固定档位 + 自定义部门集合，含"是否含子部门"开关 |
| 数据库 | MySQL |
| 会话存储 | Redis |
| Token 即时失效 | 是，`token_version` + 每请求回源数据库用户行（授权不缓存） |
| 数据权限作用范围 | 约束业务数据资源（用户、客户）；配置资源只看功能权限。客户部分见 2026-10-16 客户管理设计 D3 |
| `super_admin` 权限集 | 动态等于全部启用菜单，不落库 |
| 待分配节点 | 独立系统根节点，不参与数据范围计算与选择 |
| `POST /users` | 仅创建本地账号，飞书用户只由首次登录产生 |
| 飞书资料优先级 | 只补空值，不覆盖管理员编辑 |
| 审计日志 | 不做，下期 |
| 防提权规则 | 仅超管可授超管；不可改自身角色/部门/状态；至少保留一个超管；越权报 403 |
| 基础底座 | 见第 12 节：分层、统一响应/错误码、请求追踪、事务、Redis 使用、安全基线、可观测、工程化 |

## 15. 待确认问题

- 暂无未决问题。本轮补充的基础底座（第 12 节）待用户确认后定稿。
