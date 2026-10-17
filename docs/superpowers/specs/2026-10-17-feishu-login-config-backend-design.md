# 飞书登录配置后台化管理设计

日期：2026-10-17
状态：待用户批准（批准前不进入实现）

## 1. 意图与背景

当前飞书登录的 `APP_ID`、`APP_SECRET`、回调地址只能通过 `.env` 配置。修改凭证需要接触部署环境并重启服务，后台管理员无法自助完成，也无法在页面中确认当前使用的是哪份配置。

本设计将飞书登录凭证纳入后台集成配置能力：管理员通过后端 API 查看、保存、测试和删除配置；凭证加密保存，密钥永不回显；保存后下一次飞书登录立即使用新配置。当前仓库只负责后端，独立管理前端不在本次范围内。

## 2. 目标

- 提供飞书配置的读取、整组保存、连通性测试和删除 API。
- 配置项为 `APP_ID`、`APP_SECRET`、`REDIRECT_URI`。
- 查看接口返回当前配置来源、App ID、回调地址、更新时间和更新人；永不返回 App Secret。
- 保存前使用候选 App ID 和 App Secret 调用飞书应用凭证接口真实验证；验证失败不写库。
- App Secret 使用现有 `CONFIG_ENCRYPTION_KEY` 加密保存，数据库中不出现明文。
- 数据库配置存在时始终覆盖 `.env`；删除数据库配置后回退 `.env`。
- 已有配置更新时允许省略或留空 App Secret，沿用当前有效配置中的密钥。
- 飞书登录每次请求实时解析配置，保存后立即生效，无需重启。
- 使用独立的功能权限与后台菜单权限，不与天眼查配置共用权限。
- 保存和删除进入操作审计。

## 3. 非目标

- 实现飞书配置管理页面或其他前端代码。
- 修改前端飞书授权跳转流程；前端仍负责获取授权 `code` 并调用登录接口。
- 在线验证 `REDIRECT_URI` 是否与飞书开放平台登记值完全一致。
- 多应用、多租户或按环境维护多套飞书配置。
- OAuth 设备授权、通讯录同步或飞书组织架构同步。
- 删除 `.env` 中的向后兼容配置项。
- 配置热轮换密钥 `CONFIG_ENCRYPTION_KEY`。
- 缓存飞书配置或上游 access token。

## 4. 反目标

- 不在任何响应、日志、异常消息、审计记录或测试快照中出现 App Secret。
- 不在 App Secret 缺失、配置损坏或配置不完整时静默回退 `.env`。
- 不在上游验证成功前覆盖已有配置。
- 不让仅拥有天眼查 `integration:read` / `integration:update` 的用户管理飞书登录。
- 不因配置错误禁用本地应急超管登录。
- 不把数据库配置保存在代码、仓库、`.env.example` 或进程外未加密文件。
- 不为了读取配置引入应用级缓存或长连接。

## 5. 约束

- 沿用 FastAPI、SQLAlchemy 2.0 async、asyncmy（MySQL 8）、httpx 与 `cryptography`。
- 沿用分层：`api → services → repositories → models`；飞书 HTTP 调用封装在 `app/clients/feishu.py`。
- 沿用统一响应包装、`BusinessError`、`require_permission` 和 service 审计装饰器。
- 现有 `integration_config` 表只保存每个 provider 的一条密文，本设计复用该表，不新增迁移。
- 现有 `.env` 配置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_REDIRECT_URI` 保留为部署级回退。
- 现有飞书 OAuth 登录接口路径 `POST /api/v1/auth/feishu/login` 与请求/响应结构保持不变。
- 回调地址只接受绝对 HTTPS URL；如果为空，则在换取 `user_access_token` 时不携带 `redirect_uri`。
- 飞书官方接口要求以 `POST https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal` 校验自建应用凭证，成功响应条件为 `code == 0`。

## 6. 术语

- **数据库配置**：`integration_config.provider = "feishu"` 的记录，保存完整的、已通过上游验证的飞书配置。
- **环境配置**：由 `FEISHU_APP_ID` 与 `FEISHU_APP_SECRET` 组成且两项均非空的部署级配置；回调地址可为空。
- **有效配置**：解析时实际用于飞书登录的配置。数据库配置存在时只使用数据库配置；否则使用环境配置。
- **配置来源**：`database`、`environment` 或 `none`。
- **App Secret**：飞书应用密钥，只允许在保存请求入站时出现，不允许出站返回。

## 7. 数据模型与存储

### 7.1 复用 `integration_config`

使用现有表：

- `provider = "feishu"`
- `secret_ciphertext = Fernet(JSON payload)`
- `updated_by = 最后保存者 user_id`
- `updated_at` 由现有模型自动维护

不修改表结构，不新增迁移。

### 7.2 加密载荷

`secret_ciphertext` 解密后的 JSON 使用结构化 Pydantic 模型解析，格式为：

```json
{
  "version": 1,
  "app_id": "cli_xxx",
  "app_secret": "secret",
  "redirect_uri": "https://admin.example.com/auth/feishu/callback"
}
```

规则：

- `version` 固定为 `1`，为后续格式演进保留判别字段。
- 三个配置字段在落库前均为最终值；允许 `redirect_uri` 为空字符串，但 App ID 与 App Secret 不得为空。
- 数据库列中只保存 Fernet 密文，不另存 App ID 或回调地址明文。
- 解密成功后由服务端返回 App ID 与回调地址；App Secret 永不进入读取响应。
- 解密失败、JSON 非法、版本未知或字段不完整统一视为配置损坏，返回 `8010`，不自动删除记录，也不回退环境配置。

### 7.3 配置解析

服务提供单一的有效配置解析入口，返回不可变的 `EffectiveFeishuConfig`：

- `source`
- `app_id`
- `app_secret`
- `redirect_uri`
- `updated_at`
- `updated_by`

解析顺序：

1. 查询 `provider = "feishu"` 的数据库记录。
2. 如果记录存在，解密并完整校验：
   - 成功：返回 `source=database`。
   - 失败：返回 `8010`，终止解析，禁止回退 `.env`。
3. 如果没有数据库记录，且环境配置的 App ID 与 App Secret 均非空：
   - 返回 `source=environment`，更新时间与更新人为空。
4. 其余情况返回 `source=none`。

读取状态接口允许 `source=none` 并返回 `configured=false`；飞书登录与测试接口需要实际配置，遇到 `source=none` 返回 `8009`。

## 8. 飞书凭证验证

在 `FeishuClient` 增加应用凭证校验能力，调用：

`POST https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal`

请求：

- `Content-Type: application/json; charset=utf-8`
- JSON body 包含 `app_id` 与 `app_secret`

响应处理：

- HTTP/网络失败、非 JSON 或缺少合法 `code`：沿用 `1011` / HTTP 502，表示飞书服务不可用。
- `code != 0`：返回新增 `8011` / HTTP 422，表示 App ID 或 App Secret 未通过飞书验证。
- `code == 0`：视为凭证有效，丢弃响应中的 `app_access_token`、`tenant_access_token` 和 `expire`，不保存、不记录、不返回。

复用现有飞书客户端 5 秒连接、10 秒读取超时。错误日志只记录操作名、HTTP 状态或上游错误码，不记录请求体、App Secret 或 token。

## 9. 保存语义

`PUT` 为整组配置保存，但 App Secret 支持沿用：

- `app_id` 必填；去首尾空白后不得为空。
- `app_secret` 可省略、为 `null` 或空白。此时沿用当前有效配置的 App Secret。
- `redirect_uri` 可为空；非空时必须为绝对 HTTPS URL，不允许 fragment，最大长度 2048。保存时只去除首尾空白，不做路径、尾斜杠或大小写归一化。
- 请求模型禁止未知字段。

组合最终配置时：

1. 请求提供非空 App Secret：直接使用请求值，不读取旧密钥。这允许数据库密文损坏时用完整新配置覆盖修复。
2. 请求未提供 App Secret：
   - 数据库配置存在：解密并使用数据库配置中的 App Secret；失败返回 `8010`。
   - 数据库配置不存在但环境配置完整：沿用环境配置中的 App Secret。
   - 没有任何有效密钥：返回 `1006`，提示首次配置必须提供 App Secret。
3. 用候选 App ID 与最终 App Secret 调用飞书凭证校验。
4. 仅在验证成功后，将完整配置加密并 upsert 为 `provider="feishu"`，同一事务提交。
5. 返回保存后的状态，其中 `source=database`、App Secret 不出现。

验证失败时不得修改、插入或删除数据库记录。已有有效配置保持不变。

## 10. 删除语义

- `DELETE /integrations/feishu` 删除 `provider="feishu"` 的数据库记录并提交。
- 删除是幂等的：记录不存在也返回成功后的当前状态。
- 删除不读取或验证即将回退的环境配置。
- 删除后立即重新解析：
  - 环境配置完整：返回 `source=environment`、`configured=true`。
  - 否则：返回 `source=none`、`configured=false`。
- 删除不影响本地应急超管登录。
- 删除成功后进入审计。

## 11. API 契约

统一前缀 `/api/v1`，以下接口位于现有 integrations 路由组。

### 11.1 读取配置

`GET /integrations/feishu`

- 权限：`integration_feishu:read`
- 返回：

```json
{
  "configured": true,
  "source": "database",
  "app_id": "cli_xxx",
  "redirect_uri": "https://admin.example.com/auth/feishu/callback",
  "updated_at": "2026-10-17T12:00:00",
  "updated_by": 12
}
```

- `source=environment` 时 `updated_at`、`updated_by` 为 `null`。
- `source=none` 时 `configured=false`，其余可空字段为 `null`。
- 永不返回 App Secret。
- 数据库配置损坏时返回 `8010`，不返回半份配置。

### 11.2 保存配置

`PUT /integrations/feishu`

- 权限：`integration_feishu:update`
- 请求：

```json
{
  "app_id": "cli_xxx",
  "app_secret": "optional-on-update",
  "redirect_uri": "https://admin.example.com/auth/feishu/callback"
}
```

- 成功返回与读取接口相同的状态结构。
- 请求模型字段约束见第 9 节。
- 上游验证失败时不写库。

### 11.3 测试当前配置

`POST /integrations/feishu/test`

- 权限：`integration_feishu:update`
- 无请求体。
- 解析当前有效配置并调用飞书应用凭证校验。
- 成功返回统一成功响应；未配置返回 `8009`，损坏返回 `8010`，凭证被拒返回 `8011`，上游不可用返回 `1011`。
- 该接口不修改状态，不进入操作审计。

### 11.4 删除数据库配置

`DELETE /integrations/feishu`

- 权限：`integration_feishu:update`
- 无请求体。
- 幂等返回删除后的状态结构。
- 成功删除或幂等无操作均进入操作审计。

## 12. 登录运行时接入

`POST /api/v1/auth/feishu/login` 的现有请求与响应不变，内部改为：

1. 执行现有登录 IP 限流。
2. 通过统一解析入口读取有效飞书配置。
3. `source=none` 时返回 `8009`。
4. 使用解析出的 App ID、App Secret 和回调地址构造本次请求的 `FeishuClient`。
5. 调用现有 `exchange_code` 与 `get_user_info`。
6. 后续自动建号、资料同步、JWT 签发和登录日志行为保持不变。

不再由 API 依赖注入一个只读取全局 `.env` 的客户端。测试仍可通过显式 `client` 参数注入替身；生产路径不使用进程级飞书配置缓存。

`8009`、`8010`、`8011` 和 `1011` 都按现有登录日志机制记为飞书登录失败；登录日志 reason 使用错误码枚举名，不记录 App ID、App Secret 或上游响应正文。

## 13. 安全、审计与日志

- App Secret 仅以 Fernet 密文落库；解密仅发生在服务端内存中。
- 读取和保存响应中明确排除 App Secret；保存请求体不得进入日志或审计字段。
- 在现有日志脱敏规则中加入 `app_secret` 键及对应正则，避免结构化或格式化日志泄露。
- 飞书返回的 access token 不落库、不记录、不返回。
- 保存和删除使用现有 `@audited` service 装饰器：
  - action：`integration_feishu:update`
  - resource_type：`integration`
  - 不记录字段值
- 同步注册 `PUT` 与 `DELETE` 的路由审计元信息，保持成功和权限拒绝均按现有机制记录。
- 权限由后端强制校验，前端隐藏入口不作为安全边界。
- 独立权限避免“可改天眼查配置即可接管飞书登录”的横向越权。

## 14. 权限与菜单种子

新增权限码：

- `integration_feishu:read`：读取飞书配置状态、App ID 与回调地址。
- `integration_feishu:update`：保存、测试和删除飞书配置。

权限码符合现有 `资源:动作` 规则。`super_admin` 动态拥有全部启用权限；`default` 角色不授予。

新增菜单种子：

- `MENU`：“飞书登录配置”，路径 `/system/integrations/feishu`，父节点“系统管理”，权限 `integration_feishu:read`。
- `BUTTON`：“保存飞书配置”，父节点为飞书登录配置，权限 `integration_feishu:update`。

删除与测试不另设权限码，统一受 `integration_feishu:update` 约束。

## 15. 错误码

沿用第三方集成 `8xxx` 段：

| 业务码 | HTTP | 含义 |
| --- | --- | --- |
| 8009 | 409 | 飞书登录配置不存在或不完整 |
| 8010 | 503 | 数据库中的飞书配置无法解密或格式无效，需要重新保存 |
| 8011 | 422 | App ID 或 App Secret 被飞书拒绝 |

其他错误：

- `1006` / 422：请求格式合法但更新时没有可沿用的 App Secret，或请求字段不满足业务校验。
- `1011` / 502：飞书服务网络失败、返回非 JSON 或响应结构无法识别。

规则：

- 错误消息不透传飞书原始响应。
- 8010、1011 为 5xx，按现有监控设计进入异常日志，便于发现配置损坏和飞书不可用。
- 8011 为调用方输入错误，不进入异常日志。
- 所有错误日志和异常日志均不得包含 App Secret、请求体或飞书 access token。

## 16. 模块与改动边界

预期改动：

- `app/schemas/integration.py`：飞书请求/响应模型、配置来源枚举与内部加密载荷模型。
- `app/services/feishu_integration.py`：有效配置解析、保存、测试、删除和状态构造。
- `app/repositories/integration_config.py`：增加按 provider 删除能力。
- `app/api/integrations.py`：增加四个飞书端点并注册审计路由元信息。
- `app/clients/feishu.py`：增加应用凭证校验；支持显式配置构造。
- `app/services/auth.py`：登录前解析有效配置。
- `app/api/auth.py`：移除只读取 `.env` 的客户端依赖，保留现有请求/响应。
- `app/core/crypto.py`：解密错误码可由调用方指定，天眼查现有行为不变。
- `app/core/errors.py`：新增 8009-8011。
- `app/core/logging.py`：新增 `app_secret` 脱敏。
- `scripts/init_data.py`：新增权限菜单种子。
- 相应测试文件。

不修改 `integration_config` 表结构，不新增迁移。

## 17. 测试策略

### 17.1 配置解析

- 无数据库记录且环境配置完整：返回 `source=environment`。
- 无数据库记录且环境配置缺任一项：返回 `source=none`。
- 数据库记录有效且环境配置同时存在：数据库配置覆盖环境配置。
- 数据库密文不可解密、JSON 非法、版本未知或必填字段为空：返回 `8010`，且不回退环境配置。
- 读取状态允许未配置；登录和测试遇到未配置返回 `8009`。

### 17.2 保存

- 首次保存缺少 App Secret 返回 `1006`，不写库。
- 更新时省略或留空 App Secret，使用数据库旧密钥。
- 无数据库记录但环境配置完整时，留空密钥保存会沿用环境密钥并创建数据库配置。
- 已有数据库密文损坏但请求提供完整新密钥时，可以覆盖修复。
- 候选凭证验证成功后，落库内容为 Fernet 密文，不包含 App Secret 明文。
- 上游明确拒绝凭证返回 `8011`，网络/结构错误返回 `1011`，两者都不覆盖已有配置。
- 只更新 App ID 但旧密钥不匹配新 App ID 时，验证失败且不写库。
- 回调地址接受空值和合法 HTTPS URL；拒绝 HTTP、相对 URL、带 fragment 或超长 URL。
- 请求含未知字段返回 422。

### 17.3 状态与删除

- `GET` 返回来源、App ID、回调地址、更新时间和更新人，响应中不存在 App Secret。
- 数据库、环境和未配置三种来源状态正确。
- 删除数据库配置后立即回退环境配置。
- 环境配置不存在时删除后返回 `source=none`。
- 重复删除幂等成功。

### 17.4 客户端与登录

- 使用 `httpx.MockTransport` 断言应用凭证校验的 POST 路径、JSON 头、请求字段和成功判定。
- 飞书拒绝凭证、传输错误、非 JSON、缺少 `code` 分别映射到正确错误码。
- 飞书登录使用数据库配置覆盖环境配置。
- 保存新配置后下一次登录无需重启即可使用新值。
- 配置缺失返回 `8009`，登录日志写失败记录。
- 本地用户名密码应急登录不受飞书配置缺失或损坏影响。

### 17.5 权限、审计与脱敏

- `integration_feishu:read` 可读取，不能保存、测试或删除。
- `integration_feishu:update` 可保存、测试、删除；缺少读取权限不影响这些动作。
- 天眼查的 `integration:read` / `integration:update` 不能访问飞书配置。
- 保存和删除成功、权限拒绝均产生预期审计记录，且审计内容不含 App Secret。
- 测试接口不产生写操作审计。
- `app_secret` 在结构化日志、格式化日志和异常对象中均被脱敏。
- 菜单种子测试覆盖两个新权限码、父子关系，以及 `default` 角色无权。

## 18. 影响面与风险

- 飞书是登录入口，错误配置可能阻断飞书登录；本地应急超管是恢复路径，本设计不改变其认证方式。
- 数据库配置损坏时不回退是刻意选择：避免管理员刚保存或删除后，系统悄悄使用另一份凭证，造成来源不可解释。
- 验证凭证会调用飞书接口，但不会发送用户授权码，也不消耗天眼查额度；仍需对接口做超时与错误分类。
- 读取配置需要每次登录解密一次，30 人规模下成本可忽略；收益是配置变更立即生效。
- 回调地址无法在保存时验证与开放平台登记值完全一致，只能约束格式和协议，最终以真实授权登录为准。
- 配置更新采用“最后提交者生效”，不为 30 人规模引入并发编辑锁；并发保存时以最后一次验证成功并提交的配置为准。
- 更换 `CONFIG_ENCRYPTION_KEY` 会使飞书和天眼查密文同时不可读，属于现有全局密钥轮换限制。

## 19. 决策记录

| 决策 | 结论 |
| --- | --- |
| 配置入口 | 后端 API，前端不在本仓库范围 |
| 配置来源优先级 | 数据库存在则始终覆盖 `.env`，删除数据库记录后才回退 |
| 保存验证 | 飞书凭证验证成功后才写库 |
| 更新时 App Secret | 可留空沿用当前有效密钥；首次配置必须提供 |
| 配置损坏 | 返回 8010，不回退 `.env` |
| 删除 | 始终允许且幂等，删除后回退 `.env` 或变为未配置 |
| 权限 | 独立 `integration_feishu:read` / `integration_feishu:update` |
| 存储 | 复用 `integration_config`，Fernet 加密版本化 JSON，无迁移 |
| 配置生效 | 每次登录实时解析，无缓存、无需重启 |
| 回调地址 | 可空；非空必须为绝对 HTTPS，不做在线一致性验证 |
| 保存与删除审计 | 是，action 为 `integration_feishu:update` |
| 测试接口审计 | 否，因为不修改状态 |
| App Secret 展示 | 永不回显 |
| 本地应急超管 | 保持不变，不受飞书配置影响 |

## 20. 待用户批准

所有关键决策已与用户确认。本文档待批准后进入实现计划；批准前不写产品代码、不安装依赖、不创建迁移。
