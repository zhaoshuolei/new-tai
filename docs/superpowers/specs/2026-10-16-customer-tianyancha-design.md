# 客户管理与天眼查企业信息回填设计

日期：2026-10-16
状态：待用户批准（批准前不进入实现）

## 1. 意图与背景

内部管理后台需要客户管理能力。业务人员新建客户时，通常只知道客户的公司简称。系统应借助天眼查，让业务人员通过简称检索候选企业，人工确认后一键回填工商信息，减少手工抄录并降低录错全称、统一社会信用代码的风险。

天眼查访问凭证由超级管理员在后台配置并加密保存到数据库，不写入代码、仓库或配置文件。

## 2. 目标

- 客户档案的增删改查，受现有功能权限与数据范围约束。
- 简称检索：输入简称，返回天眼查候选企业列表（分页），并标注哪些候选已存在于本系统。
- 候选必须由业务人员人工确认，系统不做自动回填，即使只有一个候选也不自动选中。
- 一键回填：确认候选后，由后端按统一社会信用代码重新拉取工商登记信息与完整经营范围，写入客户档案；回填后允许人工修改。
- 天眼查 token 可在后台配置、替换、连通性测试，数据库中加密保存，前端永不回显明文。
- 客户以统一社会信用代码去重；已存在时返回冲突并指向已有客户，不重复调用天眼查。
- 客户负责人可显式转移。

## 3. 非目标

- 联系人、跟进记录、商机、合同等客户关联业务。
- 工商信息定时同步或一键刷新（每次刷新消耗额度，本期不做）。
- 天眼查风险、司法、知识产权等明细数据的展示与存档。
- 天眼查 OAuth 设备授权流程（见 6.1）。
- 飞书客户/通讯录同步。
- 审计日志的实现（由监控分支交付，本文只声明接入点，见 15 节）。

## 4. 反目标

- 不因"只有一个候选"而自动回填。
- 不把天眼查 token 下发到前端，不在任何响应、日志、异常信息中出现明文。
- 不信任前端提交的工商字段：回填时以服务端重新拉取的结果为准。
- 不为通用性抽象"多第三方数据源框架"；本期只对接天眼查。
- 不在搜索结果上做服务端缓存以省额度。
- 负责人转移不自动跟随部门调动：归属部门只在转移时快照，之后不随用户调岗变化。

## 5. 约束

- 沿用技术栈：FastAPI、SQLAlchemy 2.0 async、asyncmy（MySQL 8）、Alembic、Redis、httpx、`cryptography`（已在依赖中）。
- 沿用分层：`api → services → repositories → models`，外部调用封装在 `app/clients/`，与 `feishu.py` 模式一致。
- 沿用统一响应包装、`BusinessError` 异常体系与 `require_permission` 功能权限依赖。
- 错误码分段：本设计使用 `7xxx`（客户）与 `8xxx`（第三方集成）。`6xxx` 已被监控设计预留，不得占用（见 15 节）。
- 代码标识符与表名使用英文，注释与文档使用中文。
- 天眼查接入参数（来自官方文档，已实测）：
  - MCP 端点：`https://mcp.tianyancha.com/mcp`，Streamable HTTP，JSON-RPC。
  - 兼容鉴权：请求头 `Authorization: <API Key>`。
  - 计费：仅成功返回有效结果计 1 次；报错、无结果、鉴权失败、参数错误不计费。

## 6. 天眼查接入方式

### 6.1 为什么用 API Key 兼容模式

天眼查官方推荐 OAuth（Device Flow），并将 API Key 标注为"仅供不支持 OAuth 的旧客户端"。本设计选择 API Key，原因：超级管理员需要在后台直接填写并替换凭证；OAuth 设备授权依赖人工在网页确认，并产生需要刷新的 refresh token，与"后台配置并入库"的需求不匹配。

风险：官方可能收紧或下线兼容模式。届时需在后台替换为新凭证或改造为 OAuth，本设计不预先处理。

### 6.2 调用方式

后端通过 Streamable HTTP 直接以 JSON-RPC 调用 MCP，不引入 `tyc-cli` 或 `mcp-remote`。

调用序列：

1. `initialize`，取得 `Mcp-Session-Id` 响应头，后续请求回传该头。
2. `tools/call` 调用顶层工具 `search_companies`，参数 `{query, page, page_size}`，返回候选企业列表。
3. `tools/call` 调用顶层工具 `call_tool`，参数 `{company_name, tool_name, arguments}`：
   - 工商登记：`tool_name = get_company_registration_info`，`arguments = {}`。
   - 完整经营范围：`tool_name = get_historical_registration`，`arguments = {}`（见 6.4）。
   - `company_name` 可直接传统一社会信用代码（已实测）。

2026-10-16 实测结论（token 由超管提供，仅用于本次验证，未落盘）：

- 握手：`initialize` 返回 200 并下发 `Mcp-Session-Id`。`tools/list` 需要会话头，缺失时 404。
- 鉴权位置：只有 `tools/call` 校验 token。无效 token 时 `tools/call` 返回 401（`invalid_token`），`initialize` 仍返回 200。因此连通性测试必须使用 `tools/call`。无 Authorization 头时 `initialize` 返回 401。
- 顶层工具共 17 个。`get_company_registration_info` 不在顶层，必须经 `call_tool`。
- `call_tool` 不需要先调用 `get_company_capabilities`，直接调用即可返回（已对多家企业验证）。
- 检索返回：Markdown 文本（非 JSON），内含候选表，列为：`#`、企业名称、统一社会信用代码、登记状态、法定代表人、注册资本、成立日期、企业类型、企业ID、匹配类型。解析依据是列名，不是位置。
- 检索总数：正文含"总数 111 条"，只展示 `page_size` 条，需按页读取。
- 空结果：HTTP 200，正文为"空结果：未发现该维度记录"，无错误码。业务层须按文本识别。
- 工商返回：Markdown 表格，字段为中文名（统一社会信用代码、法定代表人、注册资本、核准日期、经营范围、登记机关、注册地址等），按中文字段名映射。
- 已知缺陷：工商表格中"经营范围"被上游截断，以"..."结尾（约 80 字处）。6.4 给出完整来源。
- 境外主体：候选的统一社会信用代码为"-"，登记状态为"仍注册"（如樂視網信息技術（香港）有限公司）。此类客户无法按信用代码去重与回填。
- 体积：`get_historical_registration` 单次返回约 105KB，但只计 1 次额度。

### 6.3 客户端封装

`app/clients/tianyancha.py`：

- 对外操作：`test_connection()`（内部调用 `tools/call`）、`search_candidates(keyword, page, page_size) -> (list[Candidate], total)`、`fetch_registration(credit_code) -> RegistrationInfo`、`fetch_full_business_scope(credit_code, displayed_scope) -> str | None`。
- token 作为参数传入，由 service 层解密后提供；客户端不读取数据库。
- 超时：连接 5 秒，读取 10 秒，与飞书客户端一致。
- 日志只记录操作名、HTTP 状态、天眼查错误码、耗时；不记录请求头与请求体。
- 解析：按中文字段名映射表格；字段缺失时字段值为空，不猜测。
- 上游错误映射为 `BusinessError`（见 9 节）。

### 6.4 完整经营范围的获取（D1：已决定）

问题：工商登记接口的"经营范围"字段被截断。

已验证方案：`get_historical_registration` 的 `changeList` 中，每条"经营范围"变更记录的"变更后原文"包含完整文本。以"乐视网信息技术（北京）股份有限公司"为例，2022-03-11 的一条变更记录与截断字段前缀一致，且为完整文本。

算法：

1. 取工商登记的截断经营范围，去掉结尾的"..."，得到 `displayed_prefix`。
2. 在 `changeList` 中筛选分类为"经营范围"的记录，取"变更后原文"以 `displayed_prefix` 开头的候选。
3. 取其中变更时间最新者作为完整经营范围。
4. 若没有候选（如企业从未变更过经营范围），或候选不以 `displayed_prefix` 开头，则保留截断文本，并将 `business_scope_truncated = true`。

约束：

- `changeList` 不保证覆盖全部变更事件（上游说明），因此"最新"只在已返回的记录中成立。前缀校验是防错的主要手段。
- 工商登记与历史工商各计 1 次额度，因此回填共 2 次工商相关调用（见 12 节）。

## 7. 领域模型

### 7.1 `customer`

- `id`
- `name`：企业全称（回填取工商全称；手工创建必填）
- `short_name`：检索时输入的简称，可空
- `credit_code`：统一社会信用代码，可空；非空时唯一
- `legal_person`：法定代表人，可空
- `registered_capital`：注册资本原文（如"398944.0192万人民币"），可空；不做金额换算
- `established_date`：成立日期，可空
- `business_scope`：经营范围，可空，TEXT
- `business_scope_truncated`：布尔，默认 false。为 true 表示经营范围为截断文本（见 6.4 第 4 步）
- `reg_status`：登记状态原文，可空
- `source`：枚举 `TIANYANCHA` / `MANUAL`
- `tyc_imported_at`：回填时间，可空
- `remark`：备注，可空
- `owner_user_id`：负责人用户 ID，必填，外键至 `user.id`（ON DELETE RESTRICT，停用不删除，删除需先转移）
- `owner_department_id`：归属部门快照，可空（D4）。创建或转移时取负责人当时的 `department_id`
- `created_at`、`updated_at`

约束：

- `credit_code` 唯一索引（MySQL 允许多个 NULL，境外主体可为 NULL）。
- `owner_user_id`、`owner_department_id` 建索引。

### 7.2 `integration_config`

- `id`
- `provider`：字符串，本期只有 `tianyancha`，唯一
- `secret_ciphertext`：TEXT，Fernet 加密后的 token
- `updated_by`：最后修改的用户 ID
- `updated_at`

明文 token 不落库、不缓存、不出现在任何响应中。

### 7.3 不展示 token 片段

后台只展示"已配置 / 未配置"与最后更新时间，不保存末 4 位等片段。

## 8. 加密与密钥（D2：fail-fast）

- 算法：`cryptography` 的 Fernet。
- 密钥：环境变量 `CONFIG_ENCRYPTION_KEY`，Fernet 密钥（32 字节 url-safe base64）。不入库、不入仓库，`.env.example` 给出占位。
- 启动校验（D2 已决定）：`Settings` 中该字段为必填项，缺失或格式非法（`Fernet(key)` 构造失败）时应用拒绝启动，与用户权限设计 12.7 的 fail-fast 基线一致。这意味着所有环境（含测试）都必须配置该变量。
- 轮换：更换密钥会使已存密文无法解密，需超级管理员重新保存 token。本期不做多密钥轮换。
- 解密失败（运行期出现，如数据库被手工改动）：视为"需重新配置"，返回错误 `8002`，并记录错误日志（不含密文与明文）。

## 9. 错误码

沿用 `BusinessError(code, message, http_status)`。分段：`7xxx` 客户，`8xxx` 第三方集成。

| 业务码 | HTTP | 含义 |
| --- | --- | --- |
| 7001 | 404 | 客户不存在（不可见的客户同样返回此码） |
| 7002 | 409 | 统一社会信用代码已存在（可见时附 `existing_customer_id`） |
| 7003 | 422 | 客户字段校验失败（含信用代码格式不合法） |
| 7004 | 422 | 负责人不可用（目标用户不存在、已停用或不在当前用户可见范围内） |
| 8001 | 409 | 天眼查 token 未配置 |
| 8002 | 503 | 天眼查 token 无法解密，需超管重新保存 |
| 8003 | 502 | 天眼查上游不可用或超时 |
| 8004 | 429 | 天眼查限流（上游 300004） |
| 8005 | 402 | 天眼查额度不足（上游 300006 / 300007） |
| 8006 | 502 | 天眼查鉴权失败（上游 300002/300003/300009/302004，HTTP 401） |
| 8007 | 404 | 天眼查未找到该企业（无结果） |
| 8008 | 502 | 天眼查返回内容无法解析（格式与预期不符） |

规则：

- 上游错误对前端返回的 `message` 为通用描述，不透传天眼查原文。
- 8006 使用 502 而非 401：401 保留给本系统会话失效，避免前端误判为需要重新登录。
- 8003、8006、8002、8008 均为 5xx，会按监控设计 7.4 节写入异常日志。这是预期行为，便于在后台查看天眼查的健康状况。
- 所有错误日志不含 token、Authorization 头与请求体。

## 10. 权限与数据范围

### 10.1 功能权限

- `customer:read`：列表、详情
- `customer:create`：手工创建、天眼查检索、天眼查回填
- `customer:update`：编辑、转移负责人
- `customer:delete`：删除
- `integration:read`：查看天眼查配置状态
- `integration:update`：保存 token、连通性测试

`super_admin` 自动拥有全部（动态权限集，不落库）。`default` 角色不授予上述任何权限。

### 10.2 数据范围（D3：已决定，修订用户权限设计 7.4）

客户纳入数据范围。依据用户权限设计 7.3 的统一语义，客户使用：

- 部门列：`customer.owner_department_id`
- 所有者列：`customer.owner_user_id`（SELF 档位）

实现要求：`app/services/data_scope.py` 中的过滤条件抽象为接受"部门列"与"所有者列"参数的统一函数，用户列表与客户列表共用，禁止各写一套。用户资源的行为不得改变，由既有数据范围测试兜底。

档位在客户上的效果：

- `ALL`：全部客户可见。
- `DEPT_AND_CHILD` / `DEPT` / `CUSTOM`：仅 `owner_department_id` 落在范围内的客户可见。
- `SELF`：仅 `owner_user_id` 为本人的客户可见。
- `owner_department_id` 为空或为"待分配"系统节点时，DEPT 类档位不可见（沿用用户权限设计 7.6 的 `current_department_is_business` 规则），仅 ALL 与本人（SELF）可见（D4）。

### 10.3 负责人与归属（D4、D5）

- 创建客户时，`owner_user_id` 为当前用户，`owner_department_id` 取当前用户的 `department_id`（可为空，见 10.2）。
- 负责人转移（D5：已决定，增加接口）：
  - 调用者需具备 `customer:update`，且能看到该客户。
  - 目标用户必须为 `active`，并且在调用者的用户可见范围内（复用用户数据范围过滤）。否则返回 `7004`。
  - 转移时 `owner_department_id` 重新快照为目标用户当前的 `department_id`。之后目标用户调岗不影响该客户的归属快照。
  - 转移操作记为一次写操作，进入操作日志（由监控分支实现）。
- 用户停用后其客户保留，仍由该用户（SELF）和超管可见，管理员需显式转移。

### 10.4 写操作的可见性

- 更新、删除、转移前先按数据范围校验可见性；不可见按 `7001` 处理（不泄露存在性）。
- 回填时若 `credit_code` 对应的客户已存在但当前用户不可见，返回 `7002`，省略 `existing_customer_id`。

### 10.5 候选结果的已存在标注

检索候选时，服务端查询本系统已存在的 `credit_code` 集合：可见的客户标注 `existing_customer_id`；不可见的只标注"已存在"，不给 ID。

## 11. 接口契约（设计级）

统一前缀 `/api/v1`。

客户

- `GET /customers`：分页，筛选 `keyword`（名称/简称/信用代码）、`source`；受数据范围约束。
- `POST /customers`：手工创建（`source=MANUAL`）。
- `GET /customers/{id}`、`PATCH /customers/{id}`、`DELETE /customers/{id}`。
- `PATCH /customers/{id}/owner`：转移负责人，请求体 `{owner_user_id}`（D5）。
- `GET /customers/search?keyword=&page=&page_size=`：天眼查候选检索（`customer:create`）。`page` 默认 1，`page_size` 默认 20，上限 50。响应包含 `items` 与 `total`。无候选时返回空列表，不是错误。每页计费，前端只在用户翻页时请求下一页。
- `POST /customers/import`：天眼查回填。请求体 `{credit_code, short_name?}`。服务端先校验信用代码格式并查重（查重不调用天眼查），再调用天眼查拉取工商信息与完整经营范围，写入并返回。

天眼查配置

- `GET /integrations/tianyancha`：返回 `{configured: bool, updated_at, updated_by}`，不返回任何 token 相关内容。
- `PUT /integrations/tianyancha`：请求体 `{api_key}`，加密保存；仅 `integration:update`。
- `POST /integrations/tianyancha/test`：连通性测试，仅 `integration:update`。

删除与更新约束

- 删除客户为硬删除，本期无外部引用。
- 客户字段的更新与转移不触发天眼查调用。

## 12. 关键流程

### 12.1 检索与回填

1. 业务人员输入简称，前端调用 `GET /customers/search`。
2. 服务端解密 token，调用天眼查 `search_companies`，解析候选，标注已存在客户，返回。
3. 业务人员人工选中一项（不自动选中）。
4. 前端调用 `POST /customers/import`，提交 `credit_code` 与 `short_name`。
5. 服务端：
   a. 校验信用代码格式（18 位）；不合法返回 `7003`，不调用天眼查。
   b. 按 `credit_code` 查重，已存在返回 `7002`，不调用天眼查。
   c. 调用 `call_tool(get_company_registration_info)`，传入信用代码作为 `company_name`。
   d. 校验返回的"统一社会信用代码"与请求一致，不一致返回 `8008` 且不写库。
   e. 调用 `call_tool(get_historical_registration)`，按 6.4 取完整经营范围；取不到则保留截断值并置 `business_scope_truncated = true`。
   f. 在同一事务内写入客户，`source=TIANYANCHA`、`tyc_imported_at=now`、`owner_user_id` 为当前用户。
   g. 唯一索引冲突（并发导入同一企业）捕获为 `7002`。
6. 前端回填表单，业务人员可修改后保存（回填即落库，之后的修改走 `PATCH`）。

决定：回填即落库。回填成功即消耗额度，不落库则业务人员中途离开会造成浪费且无法复用。

### 12.2 token 配置

1. 超级管理员调用 `PUT /integrations/tianyancha`。
2. 服务端用 `CONFIG_ENCRYPTION_KEY` 加密后写入 `integration_config`（按 `provider` upsert）。
3. 返回 `configured=true`，不回显 token。
4. 连通性测试：服务端解密后执行 `initialize` 并调用 `tools/call`（`search_companies`，固定查询词，`page_size=1`）。仅 `tools/call` 会校验 token。结果为成功或失败及错误分类，不返回原始上游响应。该调用消耗 1 次额度，界面需注明。

## 13. 安全

- token 加密存储；密钥在环境变量；token 不进入日志、响应、异常消息、测试快照。
- 日志脱敏：现有 `SENSITIVE_KEYS` 与 `_SENSITIVE_VALUE_PATTERN` 不包含 `api_key`，需新增。该改动落在 `app/core/logging.py`，与监控分支冲突，见 15 节。
- `PUT /integrations/tianyancha` 的请求体不记录（操作日志只记对象与动作，不记字段值，天然满足）。
- 所有新接口由 `require_permission` 与数据范围在后端校验，前端隐藏按钮不作为安全边界。
- 限流：检索与回填接口对单用户做频率限制，防止误操作耗尽额度，沿用 12.6 登录限流的 Redis 计数方式。阈值待构建者确定，需写入实现计划。

## 14. 测试策略

- 天眼查客户端：`httpx.MockTransport` 固定响应，覆盖：正常检索、空结果、限流、额度不足、鉴权失败（401）、超时、非预期格式（8008）。
- 候选与工商解析：使用实测样本脱敏后作为 fixture（至少含一个截断经营范围、一个境外主体"-"信用代码）。
- 经营范围（6.4）：前缀匹配成功取最新；无候选、前缀不符时回退截断并置标记；变更记录乱序时仍取最新。
- 回填：查重命中（不调用天眼查）、格式非法（不调用天眼查）、正常回填（2 次工商相关调用）、上游主体不一致（不写库）、并发冲突（7002）。
- 加密与启动（D2）：缺少 `CONFIG_ENCRYPTION_KEY` 或格式非法时应用拒绝启动；加解密往返；密文被改时返回 8002。
- 数据范围（D3）：客户各档位过滤；`owner_department_id` 为空或"待分配"时 DEPT 类不可见、SELF 可见（D4）；统一函数同时服务用户与客户；用户列表行为与修改前一致（回归）。
- 负责人转移（D5）：目标不可用返回 7004；目标超出调用者可见范围返回 7004；转移后归属部门快照更新；目标调岗后客户归属快照不变。
- 权限：各接口无权限返回 403；`default` 角色无法访问。
- 脱敏：保存 token 的请求体与错误日志中不出现 token 明文；`api_key` 被掩码。
- 手工验证（不进入 CI，需真实 token，由超管在本机完成）：一次真实检索、一次真实回填，确认 6.2 的结论仍成立。

## 15. 与监控/操作日志分支的协调

当前分支 `system-monitoring-logging`（worktree `.worktrees/system-monitoring-logging`）正在开发系统监控与操作日志。核查结论：两者不互相破坏，但有以下必须按顺序处理的冲突点。

1. 错误码段（已解决）：监控设计 5 节预留 `6xxx` 作为日志与监控段，本设计改用 `7xxx`/`8xxx`，不冲突。
2. 迁移链（需排序）：分支新增 `a0b141e79d98`，其 `down_revision` 为 `8db3ba95b766`，也是 master 当前唯一 head。客户与集成的迁移必须在监控迁移之后，`down_revision` 指向 `a0b141e79d98`（若监控先合并）。若两者都基于 `8db3ba95b766`，会出现双 head，`alembic upgrade head` 会失败。
3. 菜单种子与权限码测试（需协调）：监控计划在 `scripts/init_data.py` 的 `_MENU_SEEDS` 追加菜单，并在 `tests/test_seed_data.py` 中断言 `set(menus_by_code) == EXPECTED_PERMISSION_CODES`（精确相等）。客户功能会新增 6 个权限码。后合并的一方必须同时更新 `EXPECTED_PERMISSION_CODES`，否则测试失败。两边都会改这两个文件，合并时必然冲突。
4. 脱敏规则（需协调）：`api_key` 需加入 `SENSITIVE_KEYS` 与正则。`app/core/logging.py` 当前不在监控分支的改动中，因此改动可独立进行，但两者都会修改"脱敏"相关测试。
5. 配置（需协调）：`CONFIG_ENCRYPTION_KEY` 加入 `app/core/config.py` 与 `.env.example`。分支已修改这两个文件（+6 与 +12 行），合并时会冲突，且因 D2 为必填项，`tests/conftest.py` 与所有依赖 `Settings` 的测试都需提供该变量。
6. 审计接入点（需对齐）：客户的写操作需接入监控设计 7 节的 service 审计装饰器：`customer:create`、`customer:update`、`customer:delete`、`customer:transfer`（转移，新动作）、`integration:update`（保存 token）。`resource_type` 新增 `customer` 与 `integration`。`audit_log.resource_type` 为 VARCHAR(32)，无需迁移。审计不记录字段值，满足 token 不落日志的要求。
7. 异常日志（自然结果）：8002、8003、8006、8008 为 5xx，会进入监控的异常日志，属于预期。
8. 数据范围（需排序）：D3 修改 `app/services/data_scope.py` 与用户列表过滤。监控分支未改动该文件，但分支的操作日志查询接口若要受数据范围约束，需在同一统一函数上接入。本期监控设计未要求如此，本文不做要求。
9. 分支现存未提交改动：`tests/test_audit_recorder.py` 有 3 行未提交改动。这是分支作者的进行中工作，本设计不触碰。

建议合并顺序：先合并监控分支（它已有迁移与 `AuthContext` 变更，且改动面更大），客户分支基于其结果变基（rebase）。

## 16. 影响面与风险

- 天眼查返回格式可能变化。解析以中文字段名与表格列名为依据，并以 fixture 回归。
- 额度消耗：每次回填 2 次工商相关调用（工商登记 1 次 + 历史工商 1 次），另有检索每页 1 次，连通性测试 1 次。Free 版正式期每日 10 次，批量建客户会迅速耗尽，界面需展示 `8005` 并引导联系管理员。
- 数据范围改造触及用户列表的过滤函数，属于回归风险。需先保证现有用户数据范围测试全部通过，再接入客户。
- 天眼查 API Key 兼容模式可能被下线（见 6.1）。
- 工商信息是快照，回填后不自动同步。
- 经营范围：约 6.4 的算法依赖 `changeList`，它不保证覆盖全部变更。无法获取完整文本的客户会带 `business_scope_truncated=true`，界面需提示"经营范围可能不完整"。
- 境外主体无统一社会信用代码，只能手工建档，不参与去重与回填。
- 负责人转移后，归属部门为快照，可能与负责人的当前部门不一致，这是 D5 的已知取舍。
- 凭证曝光：超管在对话中以明文提供了 token。该 token 视为已泄露，需在天眼查后台轮换，并用新值替换数据库中的密文。

## 17. 迁移

- 新增 Alembic 迁移：`customer`、`integration_config` 两张表。`down_revision` 按 15 节第 2 条确定。
- 不修改既有表结构。
- 种子：新增菜单（客户管理、天眼查配置）及 `super_admin` 自动获得的权限码；`default` 不挂载。通过菜单服务或脚本写入，不手工改表。合并顺序见 15 节。

## 18. 决策记录

| 决策 | 结论 |
| --- | --- |
| 候选确认方式 | 每次人工确认，不自动回填（含只有一个候选的情况） |
| token 存储 | 数据库加密保存，Fernet，密钥来自环境变量 |
| token 展示 | 仅"已配置/未配置"与更新时间，不回显 |
| 鉴权方式 | API Key 兼容模式（见 6.1） |
| 回填即落库 | 是 |
| 回填数据来源 | 服务端按信用代码重新拉取，不信任前端工商字段 |
| 去重 | 信用代码唯一；回填前查重，命中不调用天眼查 |
| 搜索结果缓存 | 不缓存 |
| 客户数据范围（D3） | 纳入，修订用户权限设计 7.4 |
| 待分配/无部门客户（D4） | `owner_department_id` 可空；DEPT 类不可见，仅 ALL 与本人可见 |
| 密钥缺失（D2） | 启动失败（fail-fast） |
| 完整经营范围（D1） | 取 `get_historical_registration` 中前缀一致的最新经营范围变更；取不到则截断并标记 |
| 负责人转移（D5） | 增加 `PATCH /customers/{id}/owner`，归属部门重新快照 |
| 错误码段 | 客户 `7xxx`，天眼查 `8xxx`，避开监控的 `6xxx` 预留 |
| 工商信息刷新 | 本期不做 |
| 联系人/跟进等 | 本期不做 |

## 19. 待用户批准

所有决策已确定（D1-D5）。本文档待批准后进入实现计划。批准前不写产品代码、不安装依赖、不创建迁移。
