# 系统监控与日志 设计

日期：2026-10-16
状态：待确认

## 1. 意图与背景

这是"用户与权限管理后端"的下一期。企业级后台需要回答一个具体问题：**谁在什么时候改了什么。** 原设计的非目标里把"操作审计日志"明确推迟到下期，本期补上它，并一并交付同一主题下的另外三块：登录日志、系统监控、异常监控。

规模与服务场景沿用上一期：单租户内部后台，约 30 人，单实例部署，MySQL + Redis 均为本机服务，FastAPI 三层结构。

## 2. 目标

- **操作日志**：记录对业务对象的写操作（创建/修改/删除/分配等），支持按操作人、时间、动作、对象、结果查询，供审计追溯。
- **登录日志**：记录每一次登录尝试（本地与飞书）的结果、来源与原因，支持安全排查。
- **系统监控**：采集服务器 CPU/内存/磁盘与进程内存的实时快照，并按间隔落库形成历史趋势；提供应用状态（MySQL/Redis 探活）。
- **异常监控**：捕获服务端异常（未捕获异常与 5xx）落库，支持按类型/等级/时间查询、分组汇总与"已处理"标记，构成后台可查看的报警入口。
- 四块统一复用既有基础设施：统一响应体、`ErrorCode`、`AuthContext`、日志脱敏与 `request_id`。
- 所有查询接口受功能权限约束，新增权限码与菜单种子。

## 3. 非目标

- **外部告警通道**：不发飞书群、邮件、短信、Webhook。报警"先在后台看"，通道后续再加。
- **字段级变更 diff**：操作日志只到"对象 + 操作类型"粒度，不记录 `role_id: 5 → 8` 这类字段前后值。
- 分布式追踪 / APM、日志收集到 ELK、实时日志流（WebSocket/SSE）。
- 运行时动态调整日志级别。
- 多实例聚合采集（沿用单实例假设；采样任务随进程启动）。

## 4. 反目标

- 不为审计引入消息队列或异步任务框架：进程内后台任务足够，失败只记日志、不影响主请求。
- 不让审计写入与业务事务共命运：业务回滚不能导致安全相关记录一起消失。
- 不把审计降级为"接口级流水"（只记 `PATCH /users/12`）：必须能读出对象类型与对象 id。
- 不在监控里引入重型指标系统（Prometheus/Grafana）。本期只要"后台能看快照和趋势"。
- 不因监控/日志采集本身把主请求拖慢或拖挂。

## 5. 约束

- 语言/框架/分层/命名与上一期一致：Python + FastAPI，`api → services → repositories → models`，表名列名 snake_case，注释与文档中文。
- 时间统一 UTC 存 `DATETIME(6)`，API 返回 ISO8601 带时区。
- 接口前缀 `/api/v1`，成功体 `{"code": 0, "message": "ok", "data": ...}`，分页 `{"items", "total", "page", "page_size"}`。
- 业务错误码分段新增 `6xxx` 作为日志与监控段的预留（本期预计不新增业务错误码，仅占位）。
- Redis 键名统一 `{app}:{env}:{domain}:{ident}`，一律设 TTL。日志/监控数据以 MySQL 为权威，不写 Redis。
- 结构变更一律走 Alembic 迁移，禁止手工改表。

## 6. 领域模型

四张新表，均继承既有 `Base`（自带 `id`、`created_at`、`updated_at`）。

### 6.1 `audit_log`（操作日志）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | BIGINT NULL, index | 操作人；无法确定时为 NULL |
| `actor_username` | VARCHAR(64) NULL | 操作时用户名快照 |
| `actor_nickname` | VARCHAR(100) NULL | 操作时昵称快照 |
| `action` | VARCHAR(64), index | 形如 `user:update`、`dept:move` |
| `resource_type` | VARCHAR(32), index | `user` / `role` / `menu` / `department` |
| `resource_id` | BIGINT NULL, index | 目标记录 id；创建类动作成功后的新 id |
| `result` | ENUM(`success`,`denied`), index | 结果 |
| `reason` | VARCHAR(64) NULL | 拒绝原因码（如 `FORBIDDEN`、`CANNOT_MODIFY_SELF`） |
| `method` | VARCHAR(10) NULL | HTTP 方法 |
| `path` | VARCHAR(255) NULL | 请求路径 |
| `ip` | VARCHAR(64) NULL | 客户端 IP |
| `request_id` | VARCHAR(64), index | 与文件日志对账 |

索引：`(user_id, created_at)`、`(resource_type, resource_id)`、`(action, created_at)`。

### 6.2 `login_log`（登录日志）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | BIGINT NULL, index | 成功时为登录用户；失败时可能为 NULL |
| `username` | VARCHAR(100) NULL, index | 本次尝试的账号（本地账号或飞书标识不落此字段） |
| `login_type` | ENUM(`local`,`feishu`), index | 登录方式 |
| `result` | ENUM(`success`,`failure`), index | 结果 |
| `reason` | VARCHAR(64) NULL | 失败原因码：`INVALID_CREDENTIALS` / `ACCOUNT_DISABLED` / `RATE_LIMITED` / `FEISHU_UPSTREAM_ERROR` |
| `ip` | VARCHAR(64) NULL, index | 客户端 IP |
| `user_agent` | VARCHAR(512) NULL | User-Agent 快照 |
| `device_id` | VARCHAR(128) NULL | 设备标识（本地登录可传） |
| `request_id` | VARCHAR(64), index | 请求追踪 |

### 6.3 `system_metric`（系统监控采样）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cpu_percent` | FLOAT | CPU 使用率 |
| `memory_percent` | FLOAT | 内存使用率 |
| `memory_used_mb` | FLOAT | 已用内存 |
| `memory_total_mb` | FLOAT | 总内存 |
| `disk_percent` | FLOAT | 根分区使用率 |
| `disk_used_mb` | FLOAT | 根分区已用 |
| `disk_total_mb` | FLOAT | 根分区总量 |
| `process_memory_mb` | FLOAT | 本进程常驻内存 |
| `db_healthy` | BOOL | 采样时 MySQL 探活结果 |
| `redis_healthy` | BOOL | 采样时 Redis 探活结果 |
| `sampled_at` | DATETIME(6), index | 采样时刻 |

### 6.4 `exception_log`（异常日志）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `exception_type` | VARCHAR(255), index | 异常类名 |
| `message` | VARCHAR(1024) | 异常消息（经脱敏） |
| `fingerprint` | VARCHAR(64), index | 类型+归一化消息+栈顶帧的哈希，用于分组 |
| `level` | ENUM(`error`,`critical`), index | 等级 |
| `request_id` | VARCHAR(64), index | 请求追踪 |
| `method` / `path` | VARCHAR(10) / VARCHAR(255) | 出错请求 |
| `user_id` | BIGINT NULL, index | 触发用户（可确定时） |
| `status_code` | INT NULL | 返回状态码 |
| `occurred_at` | DATETIME(6), index | 发生时刻 |
| `acknowledged` | BOOL, index，默认 false | 是否已在后台标记处理 |
| `acknowledged_by` | BIGINT NULL | 处理人 |
| `acknowledged_at` | DATETIME(6) NULL | 处理时间 |

## 7. 写入机制（本次决定）

这是本设计最关键的部分。两类典型场景：

1. **业务被拒绝**（如停用超管触发 `LAST_SUPER_ADMIN`、越权触发 `CANNOT_GRANT_SUPER_ADMIN`）发生在业务事务里，最终会回滚。审计若共用该事务，就会被一起回滚，等于没记。
2. **权限依赖被拒绝**（`require_permission` 返回 403）发生在 service 被调用之前，service 层的钩子根本没有机会运行。

因此采用**双钩子 + 独立事务**：

- **独立事务**：所有日志写入走独立的 `session_factory()` 会话并自行 `commit`，与业务事务解耦。业务无论提交还是回滚，审计/异常记录都已独立落库。写入失败只记文件日志，绝不影响主请求。
- **钩子一：service 层审计装饰器**。每个写用例在 service 函数上声明一次 `action`、`resource_type` 与资源 id 来源（入参名或返回值属性）。装饰器在函数正常返回后记 `success`，在捕获 `BusinessError` 时按第 7.1 节规则决定是否记 `denied`（`reason` 取错误码名），然后原样抛出。创建类动作从返回值取新 id。
- **钩子二：全局异常处理器的权限拒绝记录**。`BusinessError` 且属于第 7.1 节的拒绝集合时，若该路由声明了审计动作，则记一条 `denied`，`resource_id` 从路径参数取（无则 NULL）。用于覆盖 `require_permission` 这类依赖层拒绝。

**去重**：钩子一记录后在被抛出的 `BusinessError` 实例上打一个标记（如 `exc.audit_recorded = True`）；钩子二遇该标记则跳过。保证同一次拒绝只有一条记录，且依赖层拒绝（service 未运行）仍被覆盖。

结果维度固定为 `success` / `denied`，查询侧可按 `result` 过滤，把越权尝试单独捞出。

请求元信息（ip、method、path、user_agent）由 `RequestContextMiddleware` 写入一个新增的请求上下文 ContextVar，供 service 装饰器与异常处理器读取；无请求上下文时（如测试直接调用）取默认空值。

**写入时效**：所有日志落库与探活调用都在请求路径上 await，但用 `asyncio.wait_for` 施加一个有界超时（默认 2 秒）。超时或写入异常只记文件日志，不影响主请求返回。数据库不可用时不阻塞请求。

### 7.1 拒绝记录的判定（本次决定）

不是所有 `BusinessError` 都值得进操作日志——把 404/422 也记进来只会淹没真正的越权线索。判定为"授权与安全拒绝"，满足其一即记 `denied`：

- `http_status == 403`（权限拒绝、防提权、不可改自身等）；或
- 错误码属于安全检查集合：`CANNOT_MODIFY_SELF`(2002)、`CANNOT_GRANT_SUPER_ADMIN`(2003)、`LAST_SUPER_ADMIN`(2004)、`SYSTEM_ROLE_IMMUTABLE`(3001)、`INVALID_MENU_HIERARCHY`(4001)、`INVALID_DEPT_MOVE`(5003)、`SYSTEM_DEPT_IMMUTABLE`(5004)。

其余业务异常（404 不存在、409 冲突、422 参数错误）不进操作日志。认证失败（401）不进操作日志，归登录日志。

### 7.2 操作人快照（本次决定）

`AuthContext` 扩展 `username` 与 `nickname` 两个字段，在 `build_auth_context`（已持有权威 `User` 行）时填充。审计装饰器从 `context` 入参取快照，不再额外查库。少数当前不接收 `context` 的写用例（如 `create_local_user`）需补上 `context` 入参以提供操作人，属本期的接口调整。

### 7.3 成功与 no-op（本次决定）

装饰器对"成功返回"一律记 `success`，包括未实际修改的 no-op 调用（如把角色设置为当前值）。语义是"该操作成功完成"，不是"确有字段变化"。理由：判断是否真正变更需要在 service 内埋点，成本高于收益，而企业审计更看重"谁在这个时刻执行了这个操作"的完整轨迹。

### 7.4 5xx 业务异常（本次决定）

除未捕获异常与 `HTTPException >= 500` 外，`BusinessError` 且 `http_status >= 500`（如缺少种子配置、飞书上游 502）也写入 `exception_log`，`level` 取 `error`。业务 4xx 不写异常日志。

登录日志不走上述钩子，而在 `login_local_user` / `login_with_feishu` 外层显式记录，覆盖整个登录调用：

- **成功**定义为该登录方式走完并签发出令牌对（`LoginResult` 返回）。仅通过凭据校验、但后续签发失败，不算成功。
- **失败**记 `failure`，`reason` 取触发异常的 `ErrorCode` 名；上面列出的四个原因码是常见情形，其余（`TOKEN_INVALID`、`INVALID_PARAMETER`、`INTERNAL_ERROR` 等）按错误码名原样落库。
- **意外异常**（非 `BusinessError`）记 `failure` + `reason=INTERNAL_ERROR`，同时进异常日志，然后原样抛出。
- `request_id` 从既有日志上下文取，`ip` / `user_agent` 取自请求上下文 ContextVar。

它天然在认证链路上，且 `user_id` 语义与操作审计不同（失败登录可能没有 user_id）。

异常监控在全局异常处理器中记录：`unhandled_exception_handler` 与 `http_exception_handler`（`status_code >= 500`）各写一条 `exception_log`，`level` 按 5xx 取 `error`。业务异常（4xx 预期内）不进异常日志。

## 8. 系统监控机制

- 新增依赖 `psutil`，用于 CPU/内存/磁盘/进程内存采集。
- **实时快照** `GET /monitor/system/current`：即时调用 psutil 采集，并实时探活 MySQL 与 Redis，不读库、不落库。
- **历史采样**：进程内 asyncio 后台任务，随 FastAPI `lifespan` 启动、随关闭停止，按 `MONITOR_SAMPLE_INTERVAL_SECONDS`（默认 60）向 `system_metric` 写一条记录。
- **应用状态**：复用 `/health/ready` 的探活逻辑，抽成一个可复用的探活函数，供采样任务与实时快照共用。
- **保留策略**：采样任务周期性（每约 1 小时）执行清理，删除超过保留天数的历史。各表保留天数独立配置，`0` 表示不清理。清理的时间依据：`system_metric` 用 `sampled_at`，`exception_log` 用 `occurred_at`，`audit_log` / `login_log` 用 `created_at`（迁移中为其补索引）。
- 后台任务默认启用，测试环境通过配置关闭，测试直接调用采样函数。

### 8.1 数据库不可用时的可见性（更正）

监控接口自身受功能权限约束，而鉴权必须读取 MySQL 中的用户与角色行。**因此 MySQL 完全不可用时，`/monitor/system/current` 与所有受保护的监控接口一样会返回 503，无法在故障期间展示 `db_healthy=false`。** 数据库故障的可见性由既有的、无需鉴权的 `/health/ready` 承担（它直接探测 MySQL 与 Redis）。

`system_metric.db_healthy` / `redis_healthy` 字段反映的是**采样时刻**的状态，用于事后回看"某段时间内应用是否健康"，不承担"数据库宕机时仍能实时报警"的职责。本设计不为此开放未鉴权的指标接口——系统指标不应对外裸露。


## 9. 接口契约

统一前缀 `/api/v1`，全部复用统一响应包装与分页形状。

操作日志（需 `audit:read`）
- `GET /logs/operations`：分页；筛选 `user_id`、`action`、`resource_type`、`resource_id`、`result`、`start_time`、`end_time`、`keyword`（操作人）。

登录日志（需 `login_log:read`）
- `GET /logs/logins`：分页；筛选 `user_id`、`username`、`login_type`、`result`、`ip`、`start_time`、`end_time`。

系统监控（需 `monitor:read`）
- `GET /monitor/system/current`：实时快照（含应用探活）。
- `GET /monitor/system/history`：分页历史采样；筛选 `start_time`、`end_time`。

异常监控（需 `exception:read`；标记处理另需 `exception:update`）
- `GET /monitor/exceptions`：分页；筛选 `exception_type`、`level`、`acknowledged`、`start_time`、`end_time`。
- `GET /monitor/exceptions/summary`：按 `fingerprint` 分组，返回发生次数、最近一次时间、等级与处理状态。
- `POST /monitor/exceptions/{id}/ack`：将**单条**异常记录标记为已处理，记录处理人与时间。汇总接口按 `fingerprint` 分组时，`acknowledged` 表示该分组下是否全部记录均已处理。

权限码（沿用 `资源:动作` 命名）：`audit:read`、`login_log:read`、`monitor:read`、`exception:read`、`exception:update`。

## 10. 菜单与种子数据

在系统管理目录下新增四个菜单，并挂按钮权限，随 `scripts/init_data.py` 幂等写入：

- 操作日志 `/system/logs/operations` → `audit:read`
- 登录日志 `/system/logs/logins` → `login_log:read`
- 系统监控 `/system/monitor` → `monitor:read`
- 异常监控 `/system/monitor/exceptions` → `exception:read`，另含"标记处理"按钮 → `exception:update`

`super_admin` 权限集动态等于全部启用菜单，自动获得以上权限；`default` 角色不挂载这些菜单。

## 11. 配置新增

`.env.example` 与 `Settings` 增加：

- `MONITOR_ENABLED`（默认 true）
- `MONITOR_SAMPLE_INTERVAL_SECONDS`（默认 60）
- `MONITOR_METRIC_RETENTION_DAYS`（默认 30）
- `EXCEPTION_LOG_RETENTION_DAYS`（默认 90）
- `LOGIN_LOG_RETENTION_DAYS`（默认 180）
- `AUDIT_LOG_RETENTION_DAYS`（默认 365）

保留天数写在我的判断里作为默认值：审计留 1 年、登录 180 天、异常 90 天、指标 30 天，均可配置关闭。

## 12. 影响面与风险

- **审计不能丢**：独立事务是硬要求；必须有测试证明业务回滚后 `denied` 记录仍在。
- **审计不能拖垮主请求**：写入失败只记日志；需处理数据库不可用时的降级。
- **权限拒绝的覆盖完整性**：依赖层 403 与 service 层 403 是两条路径，需分别有测试。
- **脱敏**：`exception_log.message` 与栈信息写入前必须经过既有 `mask_sensitive_text`；审计不记录字段值，天然降低泄露面，但仍需过滤查询入参回显。
- **采样任务生命周期**：随 `lifespan` 启停，避免测试中残留任务或关闭时报错。
- **保留策略误删**：清理任务须有测试，且 `0` 必须真正关闭清理。
- **四表增长**：单机 30 人规模可忽略，但仍按索引设计支撑时间范围查询。

## 13. 工程化与测试（必测项）

- 审计装饰器：成功记 `success`、`BusinessError` 记 `denied` 且带 `reason`、业务回滚后记录仍存在（独立事务）。
- 权限依赖 403 被记录为 `denied`。
- 登录日志：本地成功、本地失败（凭据错误/账号停用/限流）、飞书成功与上游失败各一条。
- 异常监控：未捕获异常与 5xx 各写一条，4xx 业务异常不写；`ack` 后状态与处理人正确。
- 系统监控：采样函数写入一行且探活字段正确；保留清理按天删除、`0` 不删除；`current` 在数据库不可用时仍返回指标。
- 查询接口：各筛选条件、分页、权限码校验。
- 迁移：四张新表可 upgrade/downgrade。
- 质量门：`ruff`、`mypy`、`pytest` 全绿。

## 14. 留给构建者的决定

- service 审计装饰器的具体签名与资源 id 提取方式。
- 请求上下文 ContextVar 的模块归属与字段命名。
- 采样任务的循环与清理节奏实现细节（计数触发或独立间隔）。
- 异常 `fingerprint` 的归一化算法细节。
- 查询接口的排序默认值与索引微调。

## 15. 已确认的决策记录

| 决策 | 结论 |
| --- | --- |
| 交付范围 | 操作日志 + 登录日志 + 系统监控 + 异常监控，一起做完 |
| 审计粒度 | 对象 + 操作类型，不做字段级 diff |
| 是否记拒绝操作 | 记，成功与拒绝都留痕 |
| 报警方式 | 先只在后台查看，不接外部通道 |
| 系统监控形态 | 实时快照 + 轻量定时采样落库（构建者推荐） |
| 审计写入事务 | 独立事务，业务回滚不影响记录 |
| 数据库 | MySQL（沿用） |
| 后台任务 | 进程内 asyncio 任务，随 lifespan 启停，不引入调度框架 |
| 指标依赖 | 新增 `psutil` |
| 保留策略 | 可配置天数，`0` 关闭清理 |
