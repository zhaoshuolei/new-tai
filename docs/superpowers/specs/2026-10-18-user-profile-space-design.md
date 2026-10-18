# 个人空间与员工通讯录设计

日期：2026-10-18
状态：待用户批准（批准前不进入实现）

## 1. 意图与背景

当前后端只有 `nickname`、`email`、`avatar` 三个可维护的个人字段，且唯一的写入口是管理员接口 `PATCH /api/v1/users/{id}`（需要 `user:update`）。普通员工没有任何自助维护自己资料的途径，同事之间也没有可以互相查看的入口——`GET /api/v1/users` 同样受 `user:read` 与数据范围限制。

本次把"个人资料"从管理员表格里的一行，变成员工自己的空间，并新增一个所有在职员工都能访问的通讯录作为入口：通讯录负责"找到人"，个人主页负责"了解人"，本人的同一个资料对象在编辑态下由自己维护。

本仓库只负责后端：数据持久化、API、可见性规则、审计、菜单种子与头像存储。页面在前端仓库实现。

## 2. 目标

- 员工无需任何功能权限，即可维护自己的名片区与私人区字段。
- 员工可以逐字段决定某个名片区字段是否对同事隐藏。
- 所有在职员工可以在通讯录中检索、按部门筛选并点开任一同事的个人主页。
- 同事视角只返回名片区中未被本人隐藏的字段；私人区字段永不进入同事视角。
- 头像支持本人上传，复用现有图片资源的安全存储与体积上限。
- 飞书登录只回填空字段，不覆盖员工自己维护过的昵称与头像。
- 自我资料修改与头像上传进入操作审计。
- 现有管理员用户管理体系与 `PATCH /users/{id}` 的行为保持不变。

## 3. 非目标

- 新增工号、真实姓名、职位头衔、直属上级、入职日期等权威字段，或为其配套管理端编辑接口。本期权威区只暴露 `user` 表已有的部门与账号状态，且只读。
- 管理员可配置的字段定义（自定义字段、字段顺序、字段增删）。字段清单固定在代码里，增加字段需要改代码并写迁移。
- 组织架构同步、飞书通讯录同步。
- 头像裁剪、缩放、格式转换或 CDN。
- 同事视角中的"生日只显示月日"这类字段级脱敏展示。可见性只有"可见 / 隐藏"两态。
- 个人空间内的动态、收藏、待办等扩展模块。
- **把菜单模型改成"基础菜单对全员可见"**。本期的批量授予只能让全新数据库开箱即对，无法覆盖存量部署与之后新建的自定义角色；彻底解决需要给菜单加 `is_baseline` 之类的标记并改 `get_visible_menu_tree`，属于独立改造（见 11.2）。
- 前端实现。

## 4. 反目标

- 不让自我资料接口因为缺少 `user:update` 或数据范围过滤而拒绝本人。
- 不让私人区任何字段出现在同事视角的响应、通讯录列表或日志中。
- 不让通讯录复用 `user:read`，也不让 `GET /users` 的可见范围被本次改动放宽。
- 不让本人隐藏的字段通过同事视角的空值差异被推断出来（隐藏即完全不出现）。
- 不在员工上传头像后，于下次飞书登录时被上游头像静默覆盖。

## 5. 约束

- 沿用 FastAPI、SQLAlchemy 2.0 async、asyncmy（MySQL 8）、Alembic。
- 沿用分层：`api → services → repositories → models`。
- 沿用统一响应包装 `{code, message, data}`、`BusinessError`、`require_permission` 与 `audited` 装饰器。
- 沿用 `user.avatar` 作为头像 URL 的存储位置，不新增头像字段。
- 沿用 `app/services/system_setting_asset.py` 的图片存储约束：按字节嗅探格式（PNG/JPEG/WebP/ICO，排除 SVG）、UUID 文件名、原子落盘、`system_asset_max_bytes`（默认 2 MiB）作为唯一体积上限。
- 不新增错误码；格式与参数错误使用 `INVALID_PARAMETER`（1006），目标不存在使用 `USER_NOT_FOUND`（2000）。
- 字段校验复用现有 `normalize_phone` / `normalize_email`（`app/services/contact_validation.py`）。
- `normalize_phone` 要求归一化后 7–20 位数字，适用于 `work_phone` / `personal_phone` / `emergency_contact_phone`；**`extension` 不适用**，它走独立的 1–16 位纯数字规则（见 7.1）。

## 6. 术语

- **名片区**：对同事可见的自我维护字段集合，本人可逐字段隐藏。
- **私人区**：仅本人可见、且不出现在任何同事视角响应中的字段集合。
- **权威区**：由管理员或系统维护、本人不可编辑的字段（本期为部门名与账号状态）。
- **展示名（`display_name`）**：通讯录用它称呼一个人，取 `nickname`，为空时回退 `username`。与 `nickname` 不同，它保证非空。
- **可见性覆盖**：某个员工对名片区字段的隐藏集合。默认全可见，仅记录被隐藏的字段。
- **编辑态 / 同事态**：同一份资料的两个响应视图。编辑态由 `GET /api/v1/me/profile` 提供，同事态由 `GET /api/v1/directory/{user_id}` 提供。

## 7. 数据模型与存储

### 7.1 新增表 `user_profile`

与 `user` 一对一，`user_id` 唯一外键（`ondelete="RESTRICT"`），并继承 `Base` 的 `id` / `created_at` / `updated_at`。

| 列 | 类型 | 分区 | 说明 |
|---|---|---|---|
| `user_id` | BigInteger, UNIQUE, NOT NULL | — | 指向 `user.id` |
| `signature` | String(100) | 名片 | 一句话签名 |
| `bio` | Text | 名片 | 个人简介，服务端上限 2000 字符 |
| `work_phone` | String(32) | 名片 | 工作电话，复用 `normalize_phone`（7–20 位） |
| `extension` | String(16) | 名片 | 分机号。**不复用 `normalize_phone`**，独立规则：1–16 位纯数字 |
| `work_email` | String(255) | 名片 | 工作邮箱，复用 `normalize_email`；与账号邮箱 `user.email` 语义分离 |
| `office_location` | String(100) | 名片 | 办公地点 |
| `seat` | String(50) | 名片 | 工位 |
| `expertise_tags` | JSON | 名片 | 字符串数组，最多 10 个，每个 1–20 字符 |
| `personal_links` | JSON | 名片 | 对象数组 `{label, url}`，最多 3 条，`label` 1–30 字符，`url` 为绝对 http/https 且 ≤ 500 字符 |
| `hidden_public_fields` | JSON | 可见性 | 被隐藏的名片区字段 key 数组，只允许白名单内的值 |
| `personal_phone` | String(32) | 私人 | 私人手机，复用 `normalize_phone` |
| `birthday` | Date | 私人 | 生日；可见性为仅本人 |
| `emergency_contact_name` | String(50) | 私人 | 紧急联系人 |
| `emergency_contact_phone` | String(32) | 私人 | 紧急联系电话，复用 `normalize_phone` |

`nickname` 与 `avatar` 不迁移，继续存放在 `user` 表，由本设计纳入可编辑名片区。

**长度上限一律在 pydantic 层用 `max_length` 表达，超限返回 422（`INVALID_PARAMETER`），绝不在写库时截断、也不依赖 MySQL 列宽兜底。** `bio` 的 2000 字符上限是刻意低于 `Text` 的 64KB 的：简历式长文不属于个人空间，超限应当报错而不是静默截断。

### 7.1.1 主键与标识

`user_profile.id` 是表的技术主键，**不出现在任何 API 响应中**。所有对外标识一律用 `user.id`：`/directory/{user_id}` 的路径参数、通讯录列表项的 `id`、审计的 `resource_id` 全部是 `user.id`。这避免了"列表返回 profile.id 但详情接口要 user.id"这类取错标识的错误。

### 7.2 名片区字段 key 白名单

`hidden_public_fields` 的合法值固定为：`signature`、`bio`、`work_phone`、`extension`、`work_email`、`office_location`、`seat`、`expertise_tags`、`personal_links`、`avatar`。

`nickname` 与 `department_name` 不允许隐藏：姓名与部门是"找到这个人"的最小信息，隐藏会使通讯录失去意义。

### 7.3 惰性创建与并发

不迁移期回填数据，也不在用户创建时插入 `user_profile` 行。读取时缺行按全 `NULL` / 空数组处理，首次 `PATCH` 时事务内插入（upsert）。这样迁移只新增表，不触碰既有 `user` 行。

**并发**：同一员工并发发起两次首次 `PATCH` 时，两次都会判断"行不存在"并尝试插入，第二个事务会撞上 `user_id` 唯一约束。实现必须处理 `IntegrityError`（MySQL 1062）：回滚保存点、重新读取该行、再应用本次更新，而不是把 500 抛给客户端。项目里已有同类处理先例（`app/services/user.py` 的 `_is_duplicate_username`），复用同样的判定方式。

`nickname` 的写入不需要递增 `token_version`：`AuthContext` 每次请求都从数据库权威行重建，改名不影响已签发的令牌，也不构成安全变更。不要把它接到 `_commit_user_security_change` 上，否则用户改个昵称就被强制重新登录。

### 7.4 迁移

新增一条 Alembic 迁移，`down_revision` 为当前头 `619b22fb0301`。升级只 `create_table("user_profile")` 并建 `user_id` 唯一约束；降级 `drop_table`。

### 7.5 表结构约定

- `expertise_tags` 与 `personal_links` 使用 SQLAlchemy 的 `JSON` 类型；MySQL 8 下映射为 `JSON` 列。**列本身可空，但读写一律归一化为 Python `list`**：缺行、列值为 `NULL`、空数组三者对外都是 `[]`，永不返回 `null`。upsert 时若本次未提供则写入 `[]`，不写 `NULL`。
- `hidden_public_fields` 同样为 `JSON`，同样归一化为 `[]`（即全可见），永不返回 `null`。
- 写入这三个 JSON 列的代码路径只有一个：service 层在构造/更新模型实例时赋值 Python `list`；不允许在 SQL 层拼接 JSON 表达式。
- 读取侧的统一入口是一个把 `UserProfile | None` 归一化为"全空资料"的函数，所有响应模型都从它取值，避免"行存在"与"行缺失"两条分支各自序列化。

## 8. API

所有接口挂在 `/api/v1` 下，统一响应包装。**自助接口只要求已登录，不要求任何 `permission_code`。**

### 8.1 编辑态

- `GET /api/v1/me/profile`
  返回本人完整资料：名片区全部字段、私人区全部字段、`hidden_public_fields`，以及只读区 `username`、`account_email`（即 `user.email`，账号邮箱）、`department_id`、`department_name`、`user_status`、`feishu_open_id`（用于前端判断是否飞书账号）。`nickname` 与 `avatar` 属于名片区，可编辑。`user_profile` 行缺失时返回全空结构，不返回 404。

  `account_email` 与 `department_id` 都属于只读区：前端需要它们来展示"账号邮箱（不可改）"与当前所属部门，缺了就没有数据来源。

  **不返回 `user.created_at`**：它是建号时间，不是入职日期，前端很容易把它当入职时间展示，产生错误含义。入职日期属于本期非目标。

- `PATCH /api/v1/me/profile`
  部分更新名片区与私人区字段。语义为 `exclude_unset`：未出现的字段不变，显式 `null` 清空。`expertise_tags` 与 `personal_links` 为整组替换，不提供单元素增删接口。任一字段校验失败整单 422，不做部分写入。

  该接口同时接受 `nickname`：它写入 `user.nickname`，与本接口的其余字段在同一事务内提交，失败一起回滚。这是本接口唯一跨越 `user` 表与 `user_profile` 表的写操作。`avatar` **不**通过本接口更新，只能走 `POST /me/profile/avatar`。

- `PUT /api/v1/me/profile/privacy`
  整组替换 `hidden_public_fields`。请求体 `{"hidden_fields": [...]}`，出现白名单外的 key 返回 422。重复 key 去重后保存。

- `POST /api/v1/me/profile/avatar`
  请求体为原始图片字节，`Content-Type` 必须与探测到的格式一致。成功后把返回的站内相对路径写入 `user.avatar` 并返回 `{"url": "..."}`；若替换的是本服务管理的旧头像文件，提交后 best-effort 删除旧文件。上传接口不写 `hidden_public_fields`。

  **限流**：按 `user_id` 计，每小时最多 10 次，超限返回 429（`RATE_LIMITED`）。理由是系统设置的资源上传只对管理员开放，而头像上传对全体在职员工开放，若不设限，单个账号即可持续写入 2 MiB 文件耗尽磁盘——这是本设计新引入的攻击面，必须一并封住。

  实现沿用 `app/core/rate_limit.py` 的 `enforce_rate_limit`，但该文件现有的 `tianyancha_rate_limit_key` 是天眼查专用的命名，**不要复用它**：在 `rate_limit.py` 里补一组通用常量与键构造函数，形如 `AVATAR_UPLOAD_WINDOW_SECONDS = 3600`、`AVATAR_UPLOAD_MAX_REQUESTS = 10`、`avatar_upload_rate_limit_key(user_id)`（内部调用 `app.core.redis.key("user_profile", "avatar_upload", str(user_id))`，遵循 `{app}:{env}:{domain}:{ident}` 约定）。API 层按 `customers.py` 里现成的写法注入 `get_redis` 依赖后调用。

- `DELETE /api/v1/me/profile/avatar`
  移除本人头像：把 `user.avatar` 置 `null`，提交后 best-effort 删除托管文件，返回 `{"url": null}`。

  存在的理由：`hidden_public_fields` 允许隐藏头像，但"隐藏"与"删除"是两件事。没有删除接口的话，员工一旦上传就再也回不到"没有头像"的状态，只能换一张。按 10.2 的清空语义，删除后下次飞书登录会重新回填上游头像——这与"清空即允许回填"一致，不是漏洞。

### 8.2 同事态

- `GET /api/v1/directory`
  在职员工列表。条件固定为 `deleted_at IS NULL AND status = 'active'`，**不套用数据范围过滤，不要求 `user:read`**。查询参数：`page`、`page_size`（沿用 `MAX_PAGE_SIZE`）、`keyword`（匹配 `user.nickname` 与 `user.username`，`LIKE %kw%`）、`department_id`（**精确匹配 `user.department_id`，不含下级部门**）。每项只返回 `id`、`display_name`、`nickname`、`avatar`、`signature`、`department_name`，其中 `signature` 与 `avatar` 遵守对方隐藏设置。

  这是一次 `user` LEFT JOIN `user_profile` 再 LEFT JOIN `department` 的查询。

  **展示名（固定）**：`nickname` 允许为空（8.4 与 10.2 都允许清空），但通讯录不能出现"有 id 却无法称呼"的行。因此列表项与详情都额外返回 `display_name = nickname or username`，排序也用 `COALESCE(user.nickname, user.username)` 而非 `nickname`。`nickname` 字段本身原样返回（可能就是 `null`），前端拿 `display_name` 渲染、拿 `nickname` 判断"这个人有没有设置昵称"。

  **两者皆空的退化（已知限制）**：飞书开户的 `username` 就是 `NULL`（见 `app/services/auth.py`），而本设计固定允许清空 `nickname`。若这类员工把昵称也清空，`nickname or username` 无值可回退，实现会再退一步显示 `用户{user.id}`。此时该行仍可被点开，但**按姓名与登录名都检索不到**，因为 `keyword` 只匹配这两个字段。这是"清空语义在自助与管理两条写入口上保持一致"的代价，本期接受；不做单边限制（那样会让多数飞书员工无法清空昵称，并与 10.2 的"清空后由飞书回填"冲突）。若要彻底消除，正解是给飞书账号补一个唯一登录名，属于账号身份模型变更，不在本期范围。

  这是本设计唯一会向同事暴露 `username`（登录名）的地方，只在该员工没有昵称时生效。取舍理由：`keyword` 本来就已经匹配 `user.username`，登录名在公司内部本就不是秘密；而一个没有名字的行会让通讯录失去存在意义，两害相权取暴露登录名。若之后认为登录名敏感，正确修法是把搜索也改为不匹配 `username`，而不是允许匿名行。

  排序键固定为三个，按优先级：`department.sort_order`（部门缺失时按 `NULL` 排最后）、`COALESCE(user.nickname, user.username)`、`user.id`。`user.id` 是最后的唯一兜底，保证分页稳定不重不漏。

  `department_name` 与部门排序**都不过滤部门的 `deleted_at` / `status`**：员工所属部门被删除或禁用不影响该员工仍在职这一事实，`department_name` 仍返回该部门名；只有 `department_id` 为 `NULL`（员工未分配部门）时 `department_name` 才为 `null`。这样"人还在、部门没了"不会让通讯录出现空白行。

- `GET /api/v1/directory/departments`
  通讯录的部门筛选项。返回**未删除、非系统、启用**的业务部门扁平列表（`id`、`name`、`parent_id`、`sort_order`），按 `sort_order`、`id` 排序。它存在的理由是 `GET /departments/tree` 需要 `dept:read`，本设计不放宽该权限。

  注意它与列表接口的过滤口径不同：筛选项只列"可选的有效部门"，而列表里已归属到已失效部门的员工仍然可见。这是有意的不对称——筛选器用于"找一个有效部门的人"，列表用于"看到所有在职的人"。

- `GET /api/v1/directory/{user_id}`
  同事个人主页。目标必须是在职员工，否则 `USER_NOT_FOUND`（404）。返回 `id`、`display_name`、`nickname`、`department_name`、`status`，以及名片区中所有字段。请求自己的 `user_id` 时按同事态规则返回，便于前端预览"同事看到的样子"。

  **契约（固定）**：名片区字段**始终全部出现**，被隐藏或未填写的字段一律为显式 `null`。响应不使用 `response_model_exclude_none`，也不做 key 级裁剪。这样"隐藏"与"未填写"在线上格式上完全同形，满足反目标；前端只需判断 `null` 即可决定是否渲染该行。

### 8.3 请求与响应模型

- `ProfileRead`：编辑态响应，含名片区、私人区、可见性与只读区。
- `DirectoryProfileRead`：同事态响应。名片区字段全部为 `T | None`，**不省略任何 key**，另含 `display_name`。
- `DirectoryEntryRead`：通讯录列表项，含 `id`、`display_name`、`nickname`、`avatar`、`signature`、`department_name`。
- `ProfileUpdate`：名片区 + 私人区的部分更新体，`extra="forbid"`。**不包含 `avatar`**，因此提交 `avatar` 会被 `extra="forbid"` 拒绝为 422；头像只能经 `POST /me/profile/avatar` 写入，无法绕过图片字节校验、体积上限与格式探测。
- `PrivacyUpdate`：`hidden_fields: list[str]`。
- 所有文本字段在 pydantic 中做 `strip`，空白串按 `null` 处理。
- `personal_links[].url` 只接受绝对 http/https，拒绝 `javascript:` 等协议；`bio` 不做 HTML 净化，渲染安全由前端承担。
- 响应中任何 `id` 都指 `user.id`（见 7.1.1），不暴露 `user_profile.id`。

### 8.4 输入规范化细则

这些细节单独列出来，否则实现者只能各猜一个。

- **空 `PATCH`**：请求体为 `{}`（没有任何字段）时返回 200 并回显当前资料，不写库、不产生审计记录。语义是"没有要改的"，不是错误。
- **`expertise_tags`**：整组替换前先 `strip`、丢弃空串、去重（保留首次出现的顺序），再去校验数量与长度上限。因此 `["Java", " Java "]` 存为 `["Java"]`。
- **`personal_links`**：整组替换前先 `strip`；`label` 为空串时按 `url` 的 host 兜底，host 也取不到则整条丢弃。不做 URL 去重（同一地址挂两个便于理解的标签是合理用法）。
- **`birthday`**：拒绝晚于"今天"的日期（未来生日是录入错误）。过去日期不限，包括 1900 年以前——系统不为历史数据做业务假设。

  **"今天"按 `Asia/Shanghai` 计算，不按 UTC**。理由：本系统面向国内团队，`date.today()` 在 UTC 下会让中国用户在每天 08:00 之前无法选择当天生日。代码库当前没有任何业务时区配置，时间统一按 UTC 存、按 UTC 返回，所以这里**只**为这一处引入一个固定常量，不新增可配置的时区设置：生日是纯日历日期，不做跨时区换算，该员工此刻所处时区与"今天"无关，公司所在时区就是正确口径。

  实现方式：在 `app/core/datetime_utils.py` 增加 `BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")` 与 `business_today() -> date`，供本处及其他未来需要"业务日"的逻辑复用。**不得**直接写 `date.today()`。注意 `tzdata` 不在依赖里：Linux 生产环境靠系统 tzdata 通常可用，但 Windows 开发机与精简容器可能缺失 `ZoneInfo("Asia/Shanghai")` 并抛 `ZoneInfoNotFoundError`。因此按 `try: ZoneInfo(...) except ZoneInfoNotFoundError: timezone(timedelta(hours=8))` 回退到固定 +08:00——中国自 1991 年后无夏令时，固定偏移与真实时区在本场景等价。
- **`hidden_public_fields`**：去重后保存，顺序按白名单定义顺序归一，避免同一集合因提交顺序不同产生不同的存储值与无意义的审计噪声。
- **`nickname`**：允许 `null`（清空），允许 1–100 字符，`strip` 后为空串按 `null` 处理。**不校验唯一性**——重名是真实存在的。

## 9. 可见性与响应裁剪规则

1. 编辑态永远返回全部字段，忽略 `hidden_public_fields`。
2. 同事态对每个名片区字段求值：字段在隐藏集合中，或值为空，都输出 `null`。两者不可区分，这是刻意的。
3. 私人区字段不进入任何同事态响应（`/directory` 列表与 `/directory/{id}`）。
4. 隐藏只影响展示，不影响存储。取消隐藏后原值立即恢复可见，不存在数据销毁。
5. 通讯录列表与详情使用同一份裁剪逻辑，不得各写一套。

## 10. 头像与飞书同步

### 10.1 头像存储

在 `app/services/system_setting_asset.py` 的存储基元之上新增用户头像的 asset type，落盘路径为 `<MEDIA_ROOT>/user/avatar/<uuid>.<ext>`，对外 URL 为 `<MEDIA_URL_PATH>/user/avatar/<uuid>.<ext>`，通过现有 `StaticFiles` 挂载公开可读。格式探测、原子写入与体积上限全部复用既有实现。

**`Content-Type` 缺失时的行为（固定）**：现有 `_validate_declared_content_type` 在 `content_type is None` 时直接放行（`system_setting_asset.py` 中的 `if content_type is None: return`）。头像上传**沿用这一行为，不额外加严**：缺失头放行，存在头则必须与探测格式一致。

这样定的理由有两条。一是加严必须改共享基元，而那会连带改变系统设置上传的既有契约（一个与本设计无关的接口），为了一个新接口去改别人的行为不划算。二是"缺失头放行"并不降低安全性：格式判定完全基于字节签名，`Content-Type` 只用于捕捉"声明与实际不符"这种客户端 bug，不承担安全职责。设计目标里的"必须与探测到的格式一致"精确含义是**声明了就必须对**，而不是**必须声明**。文档在此前的表述有歧义，此处以本节为准。

管理路径白名单（删除旧头像时的路径判定）必须同步支持 `/user/avatar/`，且仍拒绝非 UUID 文件名、非白名单后缀与路径穿越。

头像的删除时机与既有系统设置 asset 保持一致：**先提交 `user.avatar` 的新值，提交成功后再 best-effort 删除旧文件**。删除失败只记日志，不回滚、不报错给客户端——旧文件残留比"头像换了一半"更容易收拾。

系统设置与用户头像共用同一套存储基元、同一份体积上限配置与同一组格式白名单（PNG / JPEG / WebP / ICO），因此不存在两套审核标准。**不为用户头像另设格式子集**：ICO 对头像没有实际意义，但为它单独加一层格式过滤会额外产生一条需要测试的分支，而 ICO 不是 SVG，不带来脚本执行风险，接受它比拒绝它更省事。

### 10.2 飞书登录同步规则

`_sync_feishu_profile`（`app/services/auth.py`）当前行为：昵称与邮箱仅在为空时回填，**头像每次登录都覆盖**。本设计改为三项统一"仅在为空时回填"。

**清空语义（固定）**：判断条件统一为 `if not value`。因此员工把昵称或头像显式清成 `NULL` 后，下一次飞书登录会重新填入上游值。这是刻意选择的——个人空间里"清空"表达的是"我此刻不想展示"，而不是"我永久拒绝这个值"，后者需要一个 tombstone/来源标记列并改迁移，收益不抵成本，列入非目标。管理员用 `PATCH /users/{id}` 设的值同样会被后续飞书登录在为空时回填，但非空值不会被覆盖。

这个改动会改变现有已定义行为，`tests/test_feishu_login.py::test_avatar_and_last_login_always_refreshed` 必须改为断言"飞书头像只在本地为空时填入，已有本地头像不被覆盖"，`last_login_at` 的刷新行为保持不变。`test_empty_feishu_avatar_does_not_clear_stored_avatar` 的现有语义（上游空值不清空本地）与本次改动一致，无需修改。

## 11. 权限、菜单与审计

### 11.1 权限

本设计**不新增** `permission_code`。四个自助写入接口（`PATCH /me/profile`、`PUT /me/profile/privacy`、`POST /me/profile/avatar`、`DELETE /me/profile/avatar`）与三个 `directory` 读接口只依赖登录态。这是有意的：个人资料的自我维护与同事目录访问属于"所有在职员工的基础能力"，不应要求管理员逐个角色授权。

管理员侧 `user:read` / `user:update` 的语义与 `PATCH /users/{id}` 的可写字段都不变；`PATCH /users/{id}` 继续只写 `nickname` / `email` / `avatar`，不扩展到新的名片区与私人区字段。

### 11.2 菜单

本期新增一个顶层菜单，并解决一个现存的不一致。

- 新增顶层 `通讯录`，path `/directory`，`MenuType.MENU`，无权限码，`sort_order` 取 5（排在 `个人资料` 现有的 10 之前）。
- **`个人资料` 菜单的 path 不变**，仍是 `/profile`。前端若要区分"我的资料（编辑态）"与"同事主页"，用同一路由下的子路径承载，后端不新增菜单。

**现存问题（已验证）**：`个人资料` 菜单无权限码，却只授给了 `default` 角色。由于每个用户只有一个 `role_id`，而 `get_visible_menu_tree` 只返回该角色被授予的菜单（超管除外），任何自定义角色的员工在 `/me/menus` 里都看不到"个人资料"入口——接口能调，菜单不显示，前端在骗用户。`通讯录` 会有完全相同的问题。两个菜单都是无权限码的基础能力，入口必须与"接口对所有登录用户开放"这一事实一致。

已在测试库实测确认（探针跑完即回滚）：种子执行后 `default` 角色名下只有 `个人资料` 一个菜单；新建一个不勾任何菜单的角色，`/me/menus` 返回空数组；只勾 `user:read` 的角色仅看到 `{系统管理, 用户管理}`。

**修法（固定）**：在 `scripts/init_data.py` 中，当菜单**本次是首次被创建**时，把它一次性授予所有未删除的角色；菜单已存在则不做任何授予动作。理由是不能反过来"每次跑种子都给所有角色补授"——那样管理员在后台取消勾选后，下次部署又会被加回来，形成无解的拉锯。

- 判断"首次创建"需要改 `_get_or_create_menus`：它目前只返回 `dict[str, Menu]`，**不告诉调用方哪些菜单是本次新建的**。让它多返回一个"本次新建 key 的集合"（例如返回 `(menus_by_key, created_keys)`），在 `menu is None` 分支里把 `seed.key` 记进去。只有 `created_keys` 里的菜单参与批量授予。
- 批量授予写入 `role_menu` 关联表，对每个 `deleted_at IS NULL` 的角色插入一行；需按 `(role_id, menu_id)` 去重，避免重复执行时报唯一约束错误。

**这条修法的实际覆盖面（必须写清楚，别让人误以为解决了全部）**：菜单被创建的那一刻，库里只有 `super_admin` 与 `default` 两个角色（`init_data` 先建角色、后建菜单）。而超管本来就能看到全部启用菜单，因此"授予所有未删除角色"在全新数据库上**等价于只授给 `default`**。它不会、也不可能覆盖之后才创建的角色。

由此产生两条**已知且接受**的限制：

1. 存量部署里 `个人资料` 菜单早已存在，不会被批量补授，这批自定义角色仍然看不到它。
2. 任何**在本期上线之后**新建的自定义角色，默认也看不到 `通讯录`，直到管理员在角色的菜单授权页手动勾选。

两条都属于同一个根因：本项目的菜单可见性是"按角色显式授权"模型，没有"基础菜单对全员可见"的概念。本期**不改这个模型**——引入"系统基础菜单"需要改菜单模型、种子、`get_visible_menu_tree` 与角色授权页，是独立的一次改造，不该夹带进个人资料功能里。本期的目标是让新装的库一开始就是对的，并把改动面控制在最小。

若你希望一次解决，正确做法是独立开一期做"基础菜单"：给 `menu` 加一个 `is_baseline` 标记，`get_visible_menu_tree` 对无权限码的 baseline 菜单直接放行，不查 `role_menu`。这可以彻底消掉上述两类不一致，但会改变所有菜单的可见性语义，需要单独评审。

- `tests/test_seed_data.py::test_default_role_is_scoped_to_self` 必须改：它现在断言 `default` 角色的**唯一**菜单是 `个人资料`。改动后全新数据库里两个菜单都是本次新建，都会授给 `default`，断言改为菜单名集合 `{"个人资料", "通讯录"}`，并保留 `data_scope == SELF` 的原断言。
- `tests/test_e2e_permission_flow.py` **不受影响，已验证**：该用例的角色在 `init_data` 跑完之后才创建，那时两个菜单都已存在，不触发批量授予；它又显式只勾了 `user:read`，实测 `/me/menus` 返回 `{系统管理, 用户管理}`，与现断言一致。

### 11.3 审计

- `PATCH /me/profile`、`PUT /me/profile/privacy`、`POST /me/profile/avatar`、`DELETE /me/profile/avatar` 使用 service 层 `audited` 装饰器，`action` 分别为 `user_profile:update`、`user_profile:privacy`、`user_profile:avatar_upload`、`user_profile:avatar_delete`，`resource_type = "user_profile"`。

  `resource_id` 的取法：这四个 service 函数的签名里都显式带一个 `user_id: int` 关键字参数（由 API 层从 `AuthContext` 传入），装饰器统一用 `resource_id_param="user_id"`。**不要用 `resource_id_attr`**——头像上传/删除返回的只有 `{"url": ...}`，没有可取的 id 属性，`resource_id_attr` 会得到 `null`，把头像操作的可追溯性丢掉。统一用入参也让四个接口的审计记录形状一致。

- 通过 `register_audit_route` 注册这四条路由。注意这些路由**没有路径参数**（目标恒为当前登录用户），所以 `resource_id_param` 在这里无从取值，仅"依赖层拒绝"（例如 401 之外的权限拒绝）这条罕见路径上 `resource_id` 会是 `null`；正常成功路径的 `resource_id` 由 `audited` 从 `user_id` 入参取到，这是可接受的，因为本设计的所有接口都不要求 `permission_code`，依赖层拒绝基本不会发生。
- **401 不落审计**。现有 `is_denial` 只把 403 与少数特定错误码视为拒绝，未登录 / 令牌失效 / 账号禁用都比 `audited` 装饰器更早发生（在 `get_current_context` 依赖里），既没有 actor 也没有可归属的资源。本设计明确**不**为此修改全局 denial 判定，也不做路由级特殊处理；未认证请求的失败由现有访问日志与登录日志覆盖。
- 审计只记录动作与资源，不记录字段值，避免把私人区内容写进日志。

## 12. 测试

- 迁移：升级与降级可执行；`user_profile.user_id` 唯一约束生效。
- 自助接口：无任何 `permission_code` 的普通用户可读可写；`extra="forbid"` 拒绝未知字段；格式错误返回 422 且不产生部分写入；超长 `bio`（>2000 字符）返回 422 而非截断入库。
- 分机号：3–6 位纯数字通过校验（这是与 `normalize_phone` 的关键差异，需专门用例锁住）；含非数字或超过 16 位被拒。
- 可见性：隐藏字段在同事态返回显式 `null`；未填写字段同样为 `null`，两者线上格式不可区分；取消隐藏后原值恢复；白名单外的 key 被拒。
- `avatar` 不可经 `PATCH /me/profile` 写入：提交该字段返回 422，`user.avatar` 不变。
- 空数组归一化：新建行后 `expertise_tags`、`personal_links`、`hidden_public_fields` 均返回 `[]` 而非 `null`；行缺失与行存在两种状态对外响应完全一致。
- 私人区隔离：`/directory` 与 `/directory/{id}` 的响应中不出现任何私人字段。
- 通讯录：非在职员工（禁用、已删除）不出现，也无法通过 `{user_id}` 打开；`keyword` 与 `department_id` 过滤生效；普通员工无需 `user:read` 即可访问；员工所属部门被软删除后，该员工仍出现在列表中且 `department_name` 非 `null`。
- 昵称为空：`nickname` 为 `null` 的员工仍出现在列表中，`display_name` 回退为 `username`，且按 `username` 参与排序；`/directory/{id}` 同样返回可读的 `display_name`。
- 分页稳定性：同一 `department.sort_order` 与展示名下大量用户时，翻页不重不漏。
- 越权回归：`GET /users` 的可见范围不因本设计变宽；`PATCH /users/{id}` 仍不能写新字段。
- 头像：合法图片上传写入 `user.avatar` 并生成文件；非图片、超限、`Content-Type` 与字节不符被拒且不落盘；**缺少 `Content-Type` 时放行**（与系统设置上传一致，需专门用例锁住，防止后人误加严）；替换后旧托管文件被清理；删除接口清空 `user.avatar` 并删文件；超过每小时 10 次返回 429。
- 生日：晚于业务日（`Asia/Shanghai`）的日期被拒，等于业务日的通过；该判断不受进程时区与 UTC 影响，需覆盖 UTC 与北京时间跨日的边界用例。
- 飞书：本地已设置头像时登录不覆盖；本地为空时回填；`last_login_at` 仍每次刷新。

## 13. 验收标准

- 任意在职员工在没有 `user:read` / `user:update` 的情况下，能读取并更新自己的名片区与私人区。
- 员工能隐藏任一名片区字段，同事视角随即看到该字段变为 `null`。
- 员工能在通讯录中按姓名或登录名检索到任一在职员工，并打开其主页看到公开资料。
- 通讯录中每一行都可辨认：昵称留空的员工以登录名显示，不会出现无名行。**例外**：飞书开户且登录名为空的员工若同时清空昵称，会退化为 `用户{id}` 且无法按姓名或登录名检索，属于 8.2 记录的已知限制。

  **本期的实际达成度**：登录名存在的员工完全满足此条，并由 `test_nickname_less_employee_is_listed_and_sorted_by_username` 证明（列表回退、排序按登录名、详情回退）。飞书账号是唯一例外，取舍理由见 8.2。
- 已删除或禁用员工不出现在通讯录，且无法被直接打开。
- 私人区字段不出现在任何同事可见的响应中。
- 所有在职员工都能看到通讯录入口并按部门筛选，不论其角色是内置还是自定义。

  **本期的实际达成度**：全新安装的数据库满足此条；存量部署里已经存在自定义角色、以及本期上线后才新建的自定义角色，都需要管理员手工给角色勾选 `通讯录` 菜单才能看到入口。接口侧对所有登录用户始终开放。这条限制的根因与完整修法见 11.2，请不要把它读成"永远会自动可见"。
- 员工上传头像后，再次通过飞书登录，头像不被覆盖。
- 自我资料修改产生审计记录，且记录中不含字段值。
- 迁移可升级可降级，既有 `user` 行不被改写。

## 14. 我方决定的取舍（供复核）

- **独立 `user_profile` 表而非给 `user` 加列**：身份认证字段与资料内容字段分离，`user` 表继续只承担登录、权限、软删除这些核心语义；也让迁移无需回填。
- **可见性用 JSON 隐藏集合，而非按字段建表**：字段固定在代码里，一张字段可见性关联表只会带来 join 与一批没有实际用的行。代价是白名单校验只能在应用层做。
- **二进制可见性，不引入"仅管理员可见"第三档**：只有本人与管理员能看的字段属于账号或 HR 语义，不该混进个人空间，砍掉第三档省掉一整层判断。
- **生日只对本人生效**：曾提出"同事只看月日"的折中，但它会引入字段级的有损展示规则，与"可见性只有两态"冲突，因此降级为非目标。如果确实需要，应作为独立增量。
- **不新增权限码**：见 11.1。
- **`email` 与 `work_email` 分离**：`user.email` 继续是账号邮箱（管理员可改、飞书同步目标），同事可见的是新的 `work_email`。
- **同事态用显式 `null`，不做 key 级裁剪**：所有名片区字段始终出现在响应里，隐藏与未填写都是 `null`。这比"隐藏就删掉 key"少一层序列化逻辑，而且同样满足不可区分。
- **`avatar` 只能经上传接口写入**：`ProfileUpdate` 不含该字段，避免任意 URL 绕过图片字节、体积与格式校验。
- **`extension` 不复用 `normalize_phone`**：分机号通常 3–6 位，而 `normalize_phone` 要求 7–20 位，强行复用会把正常分机号全部拒掉。
- **JSON 列归一化为 `[]`**：行缺失、列 `NULL`、空数组三种状态对外完全一致，避免"新建行之后契约静默变化"。
- **对外只暴露 `user.id`**：`user_profile.id` 是实现细节，不出现在任何响应里，杜绝列表与详情取到不同标识。
- **头像上传限流 10 次/小时并补一个删除接口**：上传口子对全员开放，是本期唯一新增的写盘通道，不设限即可被单账号刷满磁盘；删除接口则让"回到没有头像"成为可达状态。
- **部门筛选精确匹配，排序不过滤部门状态**：筛选器只列有效部门，但列表里已归属失效部门的员工照常显示。
- **通讯录显示名回退到 `username`**：`nickname` 允许清空，但通讯录不能出现无法称呼的行。代价是"没设昵称"的员工会把登录名暴露给同事，这是刻意接受的权衡；`keyword` 本来就已匹配 `username`。
- **通讯录菜单首次创建时授予所有未删除角色**，但要说清它的真实覆盖面：菜单创建那一刻库里只有 `super_admin` 与 `default`，超管无需逐条授权，所以实际效果**等于只授给 `default`**。它不会覆盖存量部署与之后新建的自定义角色，这两类限制是已知且接受的，根因是"按角色显式授权"的菜单模型，本期不改该模型（见 11.2 关于"基础菜单"的说明）。
- **`Content-Type` 缺失时放行**：不为新接口改共享基元、不连带改变系统设置上传的既有契约；格式安全由字节签名保证。
- **生日用 `Asia/Shanghai` 判断"今天"**：`date.today()` 会让国内用户在早上 8 点前无法选当天生日；只为这一处引入固定常量，不新增可配置的时区设置。
- **飞书回填统一用 `if not value`**：清空即允许下次登录回填，不引入 tombstone 标记或来源列。
- **401 不落审计**：见 11.3。
- **惰性建行**：见 7.3。
- **`PATCH /users/{id}` 不扩字段**：避免管理员接口与自助接口的字段集合分叉成两份需要同步维护的清单。代价是管理员无法代为编辑员工的名片内容，属于已知限制。

## 15. 预期改动位置（供排期参考）

按现有分层，新增与修改大致落在以下文件。这是实现指引，不是硬约束；实现者可按实际需要拆分。

**新增**

- `migrations/versions/<rev>_create_user_profile.py`
- `app/models/user_profile.py`（`UserProfile` 模型 + 名片区字段 key 白名单常量）
- `app/repositories/user_profile.py`（`get_by_user_id`、`get_many_by_user_ids`、`add`、通讯录分页查询）
- `app/schemas/profile.py`（`ProfileRead`、`ProfileUpdate`、`PrivacyUpdate`、`DirectoryEntryRead`、`DirectoryProfileRead`、`AvatarUploadRead`）
- `app/services/user_profile.py`（编辑态读写、可见性裁剪、upsert）
- `app/services/directory.py`（通讯录列表与详情，含同一份裁剪逻辑）
- `app/services/user_avatar.py`（复用附件存储基元，处理 `user.avatar` 写入与旧文件清理）
- `app/api/profile.py`（`/me/profile` 四个接口 + 头像上传与删除）
- `app/api/directory.py`（三个读接口）

**修改**

- `app/models/__init__.py`：导出 `UserProfile`
- `app/api/router.py`：挂载两个新 router
- `app/main.py`：`OPENAPI_TAGS` 增加 `个人资料`、`通讯录` 两个标签；`tests/test_app_boot.py::test_openapi_declares_business_domain_tags` 的断言集合需同步更新
- `app/services/system_setting_asset.py`：资源类型与管理路径白名单支持 `user/avatar`（`SystemAssetType` 增加成员，`_asset_url` / `_managed_asset_path` 的路径前缀不再是写死的 `/system/`）
- `app/core/rate_limit.py`：新增头像上传的窗口/阈值常量与 `avatar_upload_rate_limit_key`
- `app/core/datetime_utils.py`：新增 `BUSINESS_TIMEZONE` 与 `business_today()`（带 `ZoneInfoNotFoundError` 回退）
- `app/services/auth.py`：`_sync_feishu_profile` 头像改为仅空值回填
- `scripts/init_data.py`：菜单种子增加 `通讯录`；菜单首次创建时批量授予所有未删除角色（取代现有的 `_ensure_default_role_menu` 单菜单逻辑）
- `tests/test_feishu_login.py`：头像覆盖相关断言反转
- `tests/test_seed_data.py`：`default` 角色菜单断言由单菜单改为集合 `{"个人资料", "通讯录"}`

**现有行为不变的保证**：`/api/v1/users` 系列接口、`UserRead`/`UserUpdate` 的字段集合、`user:read` / `user:update` 的权限语义都不动，因此 `tests/test_user_crud.py` 应保持通过。

`tests/test_e2e_permission_flow.py` **预期不受影响**：它的受限角色在 `init_data` 之后才创建，那时两个菜单都已存在，不触发批量授予，而该角色又只勾了 `user:read`，所以 `{"系统管理", "用户管理"}` 与 `isdisjoint({"角色管理", "菜单管理", "部门管理", "个人资料"})` 两个断言照旧成立。但这条推理依赖种子执行顺序，**实现后必须实跑确认**；若实际结果不同，应修正文档与代码，而不是放宽断言。

`tests/test_seed_data.py` 的 `EXPECTED_PERMISSION_CODES` 不需要改动——本设计不新增权限码，只新增一个无权限码菜单。
