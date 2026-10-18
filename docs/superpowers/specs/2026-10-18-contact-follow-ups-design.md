# 对接人跟进记录与沟通历史设计

日期：2026-10-18
状态：待批准
关联：`2026-10-17-customer-contacts-design.md`（本文实现其第 3 节非目标中的「对接人的跟进记录、沟通历史」）

## 1. 意图与背景

对接人是客户公司里的具体联系人。销售/商务每次与对接人沟通后，需要留下一条记录：什么时候、用什么方式、聊了什么、下次什么时候再跟。这些记录按时间串起来就是这个对接人的沟通历史；其中最新一条记录上的「下次跟进时间」构成该对接人当前的跟进计划。

仅有历史不够：真正会漏掉的是"该跟而没跟"。因此需要一份「今天/逾期该跟进的人」清单，覆盖调用者可见范围内所有人的计划，而不只是自己的。

术语：**跟进记录**与**沟通历史**是同一份数据的两个叫法，本文统一用「跟进记录」。

## 2. 目标

- 跟进记录属于某个对接人（因此也属于该对接人的客户），一条记录描述一次沟通。
- 每条记录包含：跟进人、跟进时间、跟进方式、沟通内容、下次跟进时间（可为空）、附件。
- 跟进人可指定为他人（例如经理替下属补录），默认当前登录用户；录入人另行留存。
- 修改、删除、恢复记录只允许该记录的跟进人本人操作。
- 「跟进方式」是可维护的字典，仅超级管理员增删改，初始为电话、微信、拜访、邮件。
- 每个对接人当前跟进计划取最新一条未删除记录上的下次跟进时间。
- 提供跨对接人的「今日到期 / 逾期」清单、按客户聚合的清单（供客户列表的「待跟进」标签页使用）、以及计数摘要（供工作台卡片使用）。
- 附件随记录保存，支持图片、PDF、Word、Excel；**必须鉴权下载**，不得通过公开静态 URL 访问。
- 记录、跟进方式采用软删除，回收站与恢复，与现有对接人口径一致。

## 3. 非目标

- 提醒推送（飞书消息、短信、站内通知、邮件）。本期只提供"查得到"的清单，不主动推送。
- 跟进记录的导出、批量操作、评论、@ 提醒、修改历史（版本记录）。
- 客户级跟进汇总页、商机、合同。
- 附件在线预览、病毒扫描、附件版本、附件跨记录复用。
- 旧版 Office 二进制格式（`.doc` / `.xls`）附件（见 6.6 与第 15 节）。
- 跟进方式的客户自定义层级（不像职能那样区分共用/自定义，全系统一份）。
- 自动物理清理已删除记录及其附件。
- 前端实现。本仓库只做后端：数据、接口、权限、审计、初始化数据。前端在另一个仓库，负责对接人详情页的记录列表与表单、客户列表的「待跟进」标签页、工作台卡片。

## 4. 反目标

- 不因跟进记录可见而放宽客户可见性；记录的可见范围永远不超过所属客户。
- 不把附件放在静态托管的媒体目录下，不提供无鉴权的下载地址。
- 不在应用日志、审计日志、异常日志中记录沟通内容、附件文件名、附件内容。
- 不因删除跟进方式而改写历史记录的方式（不迁移、不篡改）。
- 不因跟进人离职或用户被删除而放宽"只有本人能改"的规则。
- 不在列表、清单、摘要中返回已删除的记录、已删除的对接人或已删除的客户。

## 5. 约束

- 沿用现有分层：`api → services → repositories → models`。
- 沿用 `BusinessError` + `ErrorCode`、统一响应包装与 `Page` 分页、`require_permission` 依赖、`@audited` 装饰器与 `register_audit_route`。
- 错误码接续客户段 `7xxx`（见第 9 节）。
- 代码标识符与表名用英文，注释与文档用中文。
- 数据库 MySQL 8（正则/窗口函数可用）。不使用部分索引；软删除下的唯一性用生成列实现（沿用 `contact_function.active_name` 的做法）。
- 时间一律以 UTC 朴素时间入库（与现有 `DATETIME(6)` 约定一致）；「今天/逾期」按 `Asia/Shanghai` 计算（`app/core/datetime_utils.py` 已有 `BUSINESS_TIMEZONE`、`business_today`）。
- 附件上传沿用现有「原始请求体 + 按字节识别类型」的做法（`app/services/system_setting_asset.py` 的既有模式），不引入 multipart 解析依赖。
- 不新增权限码，`tests/test_seed_data.py` 的 `EXPECTED_PERMISSION_CODES` 保持不变；不新增菜单种子（见 6.4）。

## 6. 领域模型与权限

### 6.1 跟进方式 `contact_follow_up_method`

全系统一份，仅超级管理员维护。

- `id`
- `name`：名称，必填，最长 64。
- `status`：`ACTIVE` / `DELETED`，默认 `ACTIVE`（复用 `contact_status` 枚举）。
- `deleted_at`、`deleted_by`：删除时间与操作人，恢复时清空。
- `active_name`：生成列，`status = 'ACTIVE'` 时等于 `name`，否则 NULL。
- `created_by`、`updated_by`、`created_at`、`updated_at`

约束：

- 唯一索引 `(active_name)`：未删除的方式名称不重复，已删除的不占用名称。
- 不做名称与自定义职能的交叉校验（两者无关）。
- 初始数据：电话、微信、拜访、邮件，由 `scripts/init_data.py` 幂等写入（按名称存在则跳过），不写入迁移。

删除规则（与职能刻意不同）：**被任何跟进记录（含已删除记录）引用时不允许删除**，返回 `7016`；未被引用时可软删除。原因：职能删除要求迁移对接人，只是把"人挂在哪个职能"换个位置，不改变事实；而跟进方式记录的是"这次沟通实际用什么方式"，迁移会改写历史事实（把电话记录改成微信），因此不迁移、不允许删除被引用的方式。

重命名是另一回事：记录只存 `method_id`，不存方式名称快照，因此**重命名会影响所有引用它的历史记录的展示名称**。这是用户确认的选择——重命名是修正字典标签（把「微信」改成「企业微信」），不是对照写历史；若某些场合必须保留当时的叫法，应在沟通内容里写清。不做名称快照，避免每张记录表都背负一份冗余字典。

### 6.2 跟进记录 `contact_follow_up`

- `id`
- `contact_id`：必填，外键至 `customer_contact.id`，`ON DELETE RESTRICT`，建索引。对接人不会在客户之间移动（现有 `ContactUpdate` 不含 `customer_id`），因此所属客户可由对接人唯一确定，不冗余存储 `customer_id`。
- `method_id`：必填，外键至 `contact_follow_up_method.id`，`ON DELETE RESTRICT`，建索引。
- `follow_up_user_id`：跟进人，必填，外键至 `user.id`，`ON DELETE RESTRICT`，建索引。
- `follow_up_at`：跟进时间，必填，`DATETIME(6)`，建索引。
- `content`：沟通内容，必填，TEXT，长度上限 5000 字符。
- `next_follow_up_at`：下次跟进时间，可空，`DATETIME(6)`。
- `status`：`ACTIVE` / `DELETED`，默认 `ACTIVE`。
- `deleted_at`、`deleted_by`。
- `created_by`：录入人，必填，创建时取当前登录用户，之后不可改。
- `updated_by`、`created_at`、`updated_at`

索引：

- `(contact_id, status, follow_up_at)`：列表按对接人查、按时间倒序。
- `(status, next_follow_up_at)`：待跟进清单扫描。

约束与校验（服务层，失败返回 `7003`，另有说明除外）：

- `content` 去空白后非空；超过 5000 字符拒绝。
- `follow_up_at` 不得晚于当前时间 + 5 分钟（容忍时钟偏差）；允许补录过去的时间。
- `next_follow_up_at` 若提供，必须晚于 `follow_up_at`。
- `follow_up_user_id` 必须是调用者可见范围内、状态为 `ACTIVE` 的用户（与客户负责人的可见性口径一致，`apply_user_scope`），否则 `7021`。可见范围之外的跟进人不得被指定，避免通过"给不可见的人派活"泄露组织信息。
- `method_id` 必须指向未删除的方式，否则 `7014`。
- 对接人必须属于 URL 中的客户、未删除；客户必须对调用者可见且未删除，否则 `7001` / `7005`。

不变式：

- 记录引用的方式永远未删除（由 6.1 的删除规则保证，恢复记录时无需再校验方式）。
- 记录的对接人删除后，记录不可访问但保留；对接人恢复后原样可见。对接人恢复不需要处理记录。
- 客户删除后，其对接人与记录一律不可访问（`7001`）；客户恢复后原样可见。

### 6.3 附件 `contact_follow_up_attachment`

- `id`
- `follow_up_id`：必填，外键至 `contact_follow_up.id`，`ON DELETE RESTRICT`，建索引。
- `storage_path`：相对私有存储根目录的路径，形如 `contact-follow-up/<follow_up_id>/<uuid>.<ext>`，必填，唯一索引。
- `attachment.original_name`：客户端原始文件名（经 `X-File-Name` 头传入，已清洗：去掉路径分隔符与控制字符，最长 255），仅用于下载时的展示文件名；未提供时为 `null`，下载回退为 `attachment-<id>.<ext>`。
- `content_type`：按字节识别出的 MIME 类型，`String(127)`（DOCX 的 MIME 类型长 71 字符，`String(64)` 存不下）。
- `size_bytes`：文件字节数，`BIGINT`。
- `created_by`：上传者，必填（等于当时记录的跟进人）。
- `created_at`、`updated_at`

约束：

- 每条记录最多 5 个附件，单文件最大 10 MiB。超限分别返回 `7019` / `7020`。
- 附件表不做软删除：删除附件即硬删除行并尽力删除文件（与头像一致）。记录软删除时不删附件，恢复记录后附件原样可用。
- 附件不是独立审计对象之外的历史数据，不参与回收站。

### 6.4 权限

功能权限，全部复用现有权限码，不新增权限码、不新增菜单：

| 操作 | 权限 | 附加条件 |
| --- | --- | --- |
| 查看某对接人的跟进记录、记录回收站 | `customer:read` / `customer:delete`（回收站） | 客户对调用者可见，对接人未删除 |
| 新增跟进记录 | `customer:update` | 同上；跟进人须在调用者可见范围内且在职 |
| 修改跟进记录 | `customer:update` | 且调用者必须是该记录的跟进人本人 |
| 删除跟进记录 | `customer:delete` | 且必须是跟进人本人 |
| 恢复跟进记录 | `customer:delete` | 且必须是跟进人本人；客户、对接人均未删除 |
| 查看待跟进清单、按客户聚合清单、计数摘要 | `customer:read` | 结果按客户数据范围过滤 |
| 上传附件 | `customer:update` | 且必须是记录的跟进人本人 |
| 下载附件 | `customer:read` | 客户可见、对接人未删除、记录未删除 |
| 删除附件 | `customer:update` | 且必须是记录的跟进人本人 |
| 跟进方式增删改、回收站、恢复 | 超级管理员（`ctx.is_super_admin`） | 无 |

说明：

- "只有写的人能改"落实到**当前** `follow_up_user_id`：记录被转交给别人后，原跟进人不再能改，新跟进人可以。超级管理员没有例外，不能修改或删除他人记录。
- 跟进人被删除（软删除）或离职后，该记录不再有人能改、能删、能恢复（删除后只能由本人恢复，而本人已无法登录）。这是"只看本人"规则的直接后果，记录本身仍可被有 `customer:read` 的人查看。见第 14 节风险。
- 数据范围只作用于客户：记录没有独立的数据范围，永远跟随所属客户。
- 跟进方式维护接口只校验 `ctx.is_super_admin`，不新增权限码、不新增菜单；前端入口按超级管理员身份展示，与共用职能的做法一致。
- 待跟进清单默认返回可见范围内所有人的计划，支持按跟进人过滤（例如只看自己）。

### 6.5 软删除与唯一性

- 跟进记录复用 `status` + `deleted_at` + `deleted_by` 方案，列表、清单、摘要、回收站以外一律只取 `ACTIVE`。
- 跟进方式唯一性用生成列 `active_name` + 唯一索引（沿用 `contact_function.active_name`），并发插入冲突由 `IntegrityError` 兜底并转为 `7015`。
- 跟进记录没有需要唯一约束的字段（同一对接人同一天可以有多条）。

### 6.6 附件存储与文件识别

私有存储根目录：新配置项 `FOLLOW_UP_ATTACHMENT_ROOT`（默认 `attachments`，相对进程工作目录，与 `MEDIA_ROOT` 的解析方式一致）。校验沿用 `resolve_media_root` 的既有口径（非空、展开 `~`、解析为绝对路径、**不得是工作目录本身或工作目录的祖先**），并**额外要求不得与解析后的 `MEDIA_ROOT` 重叠**（相等或互为祖先/后代都拒绝），否则附件会被静态托管暴露。`MEDIA_URL_PATH` 的静态挂载点不包含该目录。新目录加入 `.gitignore`。

按字节识别（不信任文件名与前端声明的 MIME）：

| 类型 | 识别依据 | Content-Type | 扩展名 |
| --- | --- | --- | --- |
| PNG | `89 50 4E 47 0D 0A 1A 0A` | `image/png` | `.png` |
| JPEG | `FF D8 FF` | `image/jpeg` | `.jpg` |
| WebP | `RIFF....WEBP` | `image/webp` | `.webp` |
| PDF | `%PDF-` | `application/pdf` | `.pdf` |
| DOCX | ZIP 魔数 + 中央目录含 `[Content_Types].xml` 与 `word/` 前缀条目 | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `.docx` |
| XLSX | ZIP 魔数 + 中央目录含 `[Content_Types].xml` 与 `xl/` 前缀条目 | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `.xlsx` |

- ZIP 但不是 DOCX/XLSX 的一律拒绝（`7020`）。
- 声明了 `Content-Type` 时必须与识别结果匹配（沿用现有 `system_setting_asset` 的校验口径）。
- 识别 OOXML 只读取 ZIP 中央目录（`zipfile` 标准库、内存中操作，且总字节已受 10 MiB 限制），不解压、不执行任何内容。
- 旧版 `.doc` / `.xls`（OLE2 复合文档）不支持：二者魔数相同，无法在不解析复合文档流的情况下可靠区分，误标类型比拒绝更危险。本期明确不支持，见第 15 节。

### 6.7 「当前跟进计划」与待跟进口径

- 每个对接人的**当前计划** = 该对接人未删除记录中 `follow_up_at` 最大的一条（同一时刻取 `id` 最大）；该记录的 `next_follow_up_at` 即当前计划时间。
- 若最新记录没有 `next_follow_up_at`，或对接人没有任何未删除记录，则该对接人不在任何「待跟进」清单中。
- 到期判定以 `Asia/Shanghai` 自然日为准：
  - **逾期**：`next_follow_up_at < 今日 00:00:00`。
  - **今日**：`今日 00:00:00 <= next_follow_up_at < 明日 00:00:00`。
  - 两者的并集记为**待跟进**。
- 清单只包含：对接人未删除、所属客户未删除且对调用者可见。
- 一条新记录写入后，其 `next_follow_up_at`（或为空）自动成为该对接人的当前计划，旧计划被取代，不需要"完成"操作。
- 删除最新记录后，计划回退到上一条未删除记录（可能重新出现旧计划），这是软删除语义的必然结果，文档化即可。

### 6.8 并发

- 新增 / 修改记录引用方式时，对方式行加共享锁（`FOR SHARE`）；删除方式时先 `FOR UPDATE` 锁方式行，再以当前读取引用记录（`FOR UPDATE`），确认无引用后软删除。加锁顺序固定为"先方式、后记录"，与现有对接人—职能的做法一致。
- 上传附件时对记录行加 `FOR UPDATE`，锁内统计附件数量，避免并发上传突破 5 个上限；先写文件、再插行，插入或提交失败时尽力删除已写文件（沿用头像上传的回滚口径）。

## 7. 接口契约（设计级）

统一前缀 `/api/v1`。列表一律分页（`page` 默认 1，`page_size` 默认 20，上限 `MAX_PAGE_SIZE = 100`），响应为 `Page`。所有接口先校验客户可见性与对接人存在性。

### 7.1 跟进方式（仅超级管理员写）

- `GET /follow-up-methods`：未删除的方式列表，`customer:read`。不分页（数量少），按名称排序。
- `POST /follow-up-methods`：新增，请求体 `{name}`。
- `PATCH /follow-up-methods/{method_id}`：重命名，请求体 `{name}`。
- `DELETE /follow-up-methods/{method_id}`：软删除。被任何记录引用时返回 `7016`。无 `replacement` 参数——本设计不做迁移。
- `GET /follow-up-methods/recycle-bin`：已删除的方式，仅超级管理员。
- `POST /follow-up-methods/{method_id}/restore`：恢复；名称被未删除方式占用时返回 `7015`；已处于未删除状态返回 `7012`。

### 7.2 跟进记录

挂在对接人下（对接人属于 URL 中的客户）：

- `GET /customers/{customer_id}/contacts/{contact_id}/follow-ups?page=&page_size=`：未删除记录，按 `follow_up_at` 倒序、`id` 倒序。返回项含记录字段、方式名称、跟进人与录入人的展示信息。**列表第一条即当前跟进计划的来源**，前端据此展示"当前下次跟进时间"。
- `POST /customers/{customer_id}/contacts/{contact_id}/follow-ups`：新增，请求体见下。
- `PATCH .../follow-ups/{follow_up_id}`：部分更新，仅跟进人本人。可改 `follow_up_user_id`（转交）、`follow_up_at`、`method_id`、`content`、`next_follow_up_at`；`created_by` 不可改。转交后编辑权随之转移。
- `DELETE .../follow-ups/{follow_up_id}`：软删除，仅跟进人本人。
- `GET .../follow-ups/recycle-bin?page=&page_size=`：**仅返回调用者本人作为跟进人**的已删除记录，`customer:delete`。
- `POST .../follow-ups/{follow_up_id}/restore`：恢复，仅跟进人本人，客户与对接人须未删除。

请求体（新增；`content` 必填）：

```json
{
  "follow_up_user_id": 12,
  "follow_up_at": "2026-10-18T06:30:00Z",
  "method_id": 3,
  "content": "电话沟通了二期采购预算，对方下周给答复",
  "next_follow_up_at": "2026-10-21T02:00:00Z"
}
```

`follow_up_user_id` 省略时取当前登录用户；`next_follow_up_at` 省略或显式 `null` 表示暂无下次计划。

响应项：

```json
{
  "id": 100,
  "customer_id": 7,
  "contact_id": 42,
  "contact_name": "张三",
  "method_id": 3,
  "method_name": "电话",
  "follow_up_user_id": 12,
  "follow_up_user_name": "李四",
  "created_by": 12,
  "created_by_name": "李四",
  "follow_up_at": "2026-10-18T06:30:00Z",
  "content": "……",
  "next_follow_up_at": "2026-10-21T02:00:00Z",
  "attachments": [
    {
      "id": 9,
      "original_name": "报价单.pdf",
      "content_type": "application/pdf",
      "size_bytes": 182734,
      "created_at": "2026-10-18T06:31:00Z"
    }
  ],
  "created_at": "2026-10-18T06:31:00Z",
  "updated_at": "2026-10-18T06:31:00Z"
}
```

时间字段一律输出带 `+00:00` 的 UTC ISO 8601，沿用 `app/schemas/customer.py` 的 `_serialize_utc` 做法（现有客户接口即此格式，不引入新的 `Z` 写法）。

### 7.3 待跟进清单

- `GET /follow-ups/due?due=today|overdue|all&follow_up_user_id=&page=&page_size=`：按对接人一行的待跟进清单，`customer:read`。`due` 默认 `all`。返回项含：最新记录 ID、对接人与职能名称、客户 ID 与名称、跟进人、`follow_up_at`、`next_follow_up_at`、`due_state`（`today` / `overdue`）。排序：`next_follow_up_at` 升序，`id` 升序。
- `GET /follow-ups/due/customers?due=today|overdue|all&follow_up_user_id=&page=&page_size=`：按客户聚合，供客户列表的「待跟进」标签页使用。返回项：客户 ID、客户名称、`due_count`（待跟进对接人数）、`overdue_count`、`earliest_next_follow_up_at`。排序：`earliest_next_follow_up_at` 升序。
- `GET /follow-ups/due/summary?follow_up_user_id=`：工作台卡片计数，返回 `{ "today": n, "overdue": m }`，统计单位是**对接人**（同一对接人只计一次），按可见范围过滤。

`follow_up_user_id` 过滤对三个接口一致生效（可用来只看自己）。

### 7.4 附件

- `POST /customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/attachments`：原始文件字节作为请求体（与系统设置资源上传一致，不用 multipart），`Content-Type` 需与识别结果匹配；原始文件名放在 `X-File-Name` 请求头（用户代理按 UTF-8 百分号编码，缺失或无法解码时存 `null`，下载时回退为 `attachment-<id>.<ext>`）。仅跟进人本人，`customer:update`。返回附件元数据。**文件名不走查询字符串**，因为 Uvicorn 与上游代理的访问日志会记录完整查询串，而附件名属于业务内容。
- `GET /customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/attachments/{attachment_id}/download`：鉴权下载，`customer:read`。响应 `Content-Disposition: attachment; filename*=UTF-8''<encoded original_name>`，`Content-Type` 为识别出的类型，`Cache-Control: no-store`。校验顺序同记录可见性。
- `DELETE /customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/attachments/{attachment_id}`：硬删除附件与文件，仅跟进人本人，`customer:update`。

## 8. 关键流程

### 8.1 新增记录

1. 校验客户可见且未删除（`7001`）。
2. 校验对接人属于该客户且未删除（`7005`）。
3. 校验方式未删除（`7014`，并对方式行加共享锁）。
4. 解析跟进人：缺省为当前登录用户；显式指定时校验为可见范围内在职用户（`7021`）。
5. 校验 `content`、`follow_up_at`、`next_follow_up_at`（`7003`）。
6. 写入，`created_by = context.user_id`，审计 `contact_follow_up:create`。

### 8.2 修改记录

校验可见性与存在性后，校验调用者是当前跟进人（否则 `7017`）。字段校验同 8.1；修改 `follow_up_user_id` 时按 8.1 第 4 步重新校验目标用户。审计 `contact_follow_up:update`。

### 8.3 删除与恢复

- 删除：可见性、存在性、跟进人本人（`7017`）→ 置 `DELETED`、写 `deleted_at`/`deleted_by`，审计 `contact_follow_up:delete`。附件保留。
- 恢复：客户与对接人须未删除，记录须处于已删除状态（否则 `7012`），调用者须是跟进人本人（`7017`），审计 `contact_follow_up:restore`。

### 8.4 待跟进清单

1. 以窗口函数取每个对接人 `follow_up_at` 最大的未删除记录（`row_number() over (partition by contact_id order by follow_up_at desc, id desc) = 1`）。
2. 过滤 `next_follow_up_at IS NOT NULL`，按 `due` 参数与业务时区换算出的今日边界筛选。
3. 连接对接人（未删除）、客户（未删除），应用 `apply_customer_scope`。
4. 可选按 `follow_up_user_id` 过滤。
5. 分页返回；按客户聚合与摘要复用同一 CTE。

### 8.5 附件上传

1. 可见性、存在性、跟进人本人（`7017`）。
2. 对记录行 `FOR UPDATE`，锁内统计附件数（≥5 返回 `7019`）。
3. 流式读取请求体，超过 10 MiB 中途终止（`7020`）。
4. 按 6.6 识别类型并校验声明类型（`7020`）。
5. 生成 UUID 文件名，原子写入私有目录；插行；提交。
6. 提交失败时尽力删除已写文件；审计 `contact_follow_up:attachment_upload`（只记记录 ID 与附件 ID，不记文件名）。

### 8.6 删除跟进方式

`FOR UPDATE` 锁方式行 → 以当前读取统计引用记录（含已删除记录）→ 有引用返回 `7016`；无引用则软删除，审计 `follow_up_method:delete`。

### 8.7 对接人、客户删除的影响

- 对接人软删除：其记录不可访问、不进待跟进清单；恢复对接人后原样可见，无需处理记录。
- 客户软删除：其对接人、记录、待跟进项一律不可访问；恢复客户后原样可见。
- 用户软删除：其作为跟进人的记录仍可被有权限者查看，但无人能改（见 6.4）。

## 9. 错误码

接续客户段，沿用 `BusinessError(code, message, http_status)`：

| 业务码 | HTTP | 含义 |
| --- | --- | --- |
| 7013 | 404 | 跟进记录不存在、已删除，或不属于该对接人 |
| 7014 | 404 | 跟进方式不存在或已删除 |
| 7015 | 409 | 跟进方式名称重复 |
| 7016 | 409 | 跟进方式已被记录引用，不可删除 |
| 7017 | 403 | 调用者不是该记录的跟进人，无权修改、删除、恢复或增删附件 |
| 7018 | 404 | 附件不存在或不属于该记录 |
| 7019 | 422 | 附件数量超过每条记录 5 个 |
| 7020 | 422 | 附件类型不支持、内容与声明类型不符，或超过 10 MiB |
| 7021 | 422 | 指定的跟进人不存在、不在可见范围内或非在职 |

字段校验（必填、超长、时间范围、`next_follow_up_at` 早于 `follow_up_at`）沿用 `7003`。客户不可见/已删除 `7001`，对接人不存在/已删除 `7005`，未删除状态恢复 `7012`。

## 10. 待确认项

已由用户确认：

1. 跟进记录与沟通历史是同一份数据。
2. 记录字段：跟进人、跟进时间、跟进方式、沟通内容、下次跟进时间。
3. 下次跟进时间带一个到期清单；清单覆盖可见范围内所有人。
4. 记录可改可删。
5. 下次跟进时间精确到时刻。
6. 跟进方式可维护，超级管理员增删改，初始电话/微信/拜访/邮件。
7. 只有跟进人本人能改、能删。
8. 跟进人可指定为他人（区分跟进人与录入人）。
9. 待跟进清单入口：客户列表标签页 + 工作台卡片（前端另一仓库）。
10. 支持附件与截图；附件必须鉴权下载。
11. 附件上限：单文件 10 MiB、每条记录 5 个。
12. 跟进方式重命名后，历史记录显示新名字（不做名称快照）。

我的决定（用户可在批准时否决）：

1. 跟进方式被引用时不可删除，不迁移历史（6.1）。
2. 跟进人可转交，转交后编辑权随之转移（7.2）。
3. 回收站只展示本人作为跟进人的记录（7.2）。
4. 待跟进清单按对接人一行、按客户聚合另行提供，摘要统计单位为对接人（7.3）。
5. 不支持旧版 `.doc` / `.xls`（6.6）。
6. 跟进人不可见/非在职返回 `7021`；错误码 7013–7021 分配（第 9 节）。
7. 附件删除为硬删除、记录软删除保留附件（6.3）。
8. 不新增权限码与菜单，方式维护按超级管理员身份控制（6.4）。
9. 沟通内容上限 5000 字符，跟进时间不允许晚于当前 5 分钟以上（6.2）。
10. 客户列表的「待跟进」标签页由独立聚合接口支撑，不改造 `GET /customers`（7.3）。

## 11. 安全与审计

- 审计 action：`contact_follow_up:create`、`contact_follow_up:update`、`contact_follow_up:delete`、`contact_follow_up:restore`、`contact_follow_up:attachment_upload`、`contact_follow_up:attachment_delete`、`follow_up_method:create`、`follow_up_method:update`、`follow_up_method:delete`、`follow_up_method:restore`。
- 审计 `resource_type`：记录为 `contact_follow_up`，方式为 `contact_follow_up_method`。
- 审计只记录对象与动作，不记录沟通内容、跟进方式名称、附件文件名或附件内容。
- 附件下载必须携带有效登录态、客户可见、对接人未删除、记录未删除；不使用静态 URL，不返回可直接外链的地址。
- 附件文件名下载时经 `filename*=UTF-8''` 编码，避免响应头注入；上传时原始文件名经 `X-File-Name` 头传递并清洗（去路径分隔符与控制字符、限长 255），不经查询字符串，避免出现在代理访问日志中；存储路径由服务端生成，客户端文件名从不参与路径拼接。
- 附件类型按字节判定，防止把可执行/脚本文件伪装成图片或 Office 文档后由浏览器渲染；全局 `X-Content-Type-Options: nosniff` 保留。
- 应用日志、异常消息中不回显沟通内容与附件文件名。

## 12. 测试策略

- 权限：新增记录需 `customer:update`；删除/恢复需 `customer:delete`；非跟进人修改、删除、恢复、上传附件返回 `7017`，超级管理员也不例外；跟进方式写操作非超级管理员返回 403；`default` 角色全部不可访问。
- 可见性：客户不可见或已删除时，记录列表、回收站、待跟进清单不返回该客户数据；对接人已删除返回 `7005`。
- 跟进人校验：指定不可见用户、已删除用户、非在职用户返回 `7021`；缺省时取当前登录用户；转交后原跟进人返回 `7017`、新跟进人可改。
- 字段校验：`content` 空白、超 5000；`follow_up_at` 晚于当前 5 分钟以上；`next_follow_up_at` 早于或等于 `follow_up_at`；方式不存在或已删除 `7014`。
- 软删除：删除后列表与待跟进清单不再返回；回收站仅返回本人记录；恢复后可见；恢复未删除记录返回 `7012`；删除最新记录后计划回退到上一条。
- 待跟进口径：今天/逾期边界按 `Asia/Shanghai` 计算（用固定 `next_follow_up_at` 覆盖今日 00:00、昨日 23:59、明日 00:00）；空 `next_follow_up_at` 不进清单；同一对接人只取最新记录；旧记录的过期计划不影响当前计划；三接口 `due` 过滤、`follow_up_user_id` 过滤、排序稳定；摘要计数为对接人数；分页边界与大页码。
- 跟进方式：名称重复 `7015`、并发插入冲突转 `7015`、已删除不占用名称、被引用不可删 `7016`（含仅被已删除记录引用）、无引用可删、恢复名称冲突 `7015`、恢复未删除 `7012`。
- 附件：合法 PNG/JPEG/WebP/PDF/DOCX/XLSX 上传成功且文件名随机；伪装扩展名、ZIP 非 OOXML、超 10 MiB、声明类型不符返回 `7020`；第 6 个附件返回 `7019`；下载需登录且受可见性约束，`Content-Disposition` 与 `Content-Type` 正确；删除附件后文件不存在；记录软删除后附件仍可恢复访问。
- 存储安全：附件根目录与 `MEDIA_ROOT` 重叠时配置校验拒绝启动；附件不出现在 `/media/...` 静态路径下。
- 初始化：`init_data` 重复运行不产生重复方式；不新增权限码（`EXPECTED_PERMISSION_CODES` 不变）。
- 迁移：空库升级与回滚；三张表与外键、索引、生成列在 MySQL 上生效。
- 日志：审计与异常日志中不出现沟通内容、附件文件名。

## 13. 迁移

- 新增三张表：`contact_follow_up_method`、`contact_follow_up`、`contact_follow_up_attachment`。
- 不修改既有表。`down_revision` 指向当前唯一 head `26a11cef913c`（用户资料迁移）。
- 不新增权限码与菜单，`tests/test_seed_data.py` 不变。
- 新增配置：`FOLLOW_UP_ATTACHMENT_ROOT`（默认 `attachments`）、`FOLLOW_UP_ATTACHMENT_MAX_BYTES`（默认 10485760）、`FOLLOW_UP_ATTACHMENT_MAX_PER_RECORD`（默认 5）；同步 `.env.example` 与 `.gitignore`。
- 初始跟进方式由 `scripts/init_data.py` 幂等写入，不写迁移。

## 14. 影响面与风险

- 跟进人被软删除后其记录永久只读，且删除后无人能恢复。这是"只有本人能改"的代价；若日后需要管理员纠正历史，必须改规则而不是加旁路。
- 附件落在本地私有目录，沿用本地存储的部署约束：生产必须单副本或多副本挂同一持久目录，扩容前需要对象存储。
- 待跟进清单需要窗口函数与跨表连接，对接人与记录规模大时需要分页索引支撑；本期按单客户/单部门规模设计。
- 软删除使数据只增不减；附件目录不会自动清理，删除记录不清理附件，需要未来的清理任务。
- 「当前计划」依赖最新记录，补录历史记录可能改变"最新"的判定（按 `follow_up_at` 取最大），这是刻意的：补录的旧沟通不应覆盖更新的计划。
- 错误码已用到 7021，后续功能应从 7022 接续。

## 15. 决策记录

| 决策 | 结论 | 来源 |
| --- | --- | --- |
| 跟进记录与沟通历史 | 同一份数据 | 用户确认 |
| 记录归属 | 对接人，因此属于其客户 | 用户确认 |
| 记录字段 | 跟进人、跟进时间、方式、内容、下次跟进时间、附件 | 用户确认 |
| 跟进人默认与可改性 | 默认本人，可指定他人；另存录入人 | 用户确认 |
| 编辑/删除权 | 仅跟进人本人，超管无例外 | 用户确认 |
| 可改可删 | 是，软删除 + 回收站 + 恢复 | 用户确认 |
| 下次跟进时间精度 | 精确到时刻 | 用户确认 |
| 跟进方式维护 | 超级管理员可维护，初始四项 | 用户确认 |
| 待跟进清单范围 | 可见范围内所有人，可按人过滤 | 用户确认 |
| 清单入口 | 客户列表标签页 + 工作台卡片 | 用户确认 |
| 附件 | 图片、PDF、Word、Excel；10 MiB；每条 5 个 | 用户确认 |
| 附件访问 | 鉴权下载，静态目录不暴露 | 用户确认 |
| 方式删除与历史 | 被引用不可删，不迁移、不改写历史 | 我的决定 |
| 方式重命名与历史 | 记录只存 `method_id`，重命名影响历史展示（显示新名字），不做名称快照 | 用户确认 |
| 附件文件名传递 | 走 `X-File-Name` 请求头，不经查询字符串 | 我的决定 |
| 跟进人转交 | 支持，编辑权随转交转移 | 我的决定 |
| 回收站范围 | 仅本人作为跟进人的记录 | 我的决定 |
| 待跟进聚合 | 对接人清单 + 客户聚合 + 摘要三接口 | 我的决定 |
| 旧版 Office 附件 | 不支持 `.doc` / `.xls` | 我的决定 |
| 附件删除 | 硬删除文件与行；记录软删除保留附件 | 我的决定 |
| 权限与菜单 | 不新增权限码与菜单；方式维护按超管身份 | 我的决定 |
| 客户列表改造 | 不改 `GET /customers`，新增聚合接口 | 我的决定 |
| 私有附件目录 | 新增 `FOLLOW_UP_ATTACHMENT_ROOT`，禁止与 `MEDIA_ROOT` 重叠 | 我的决定 |
| 待跟进时区口径 | `Asia/Shanghai` 自然日边界 | 我的决定 |
| 沟通内容上限 | 5000 字符 | 我的决定 |
| 跟进时间未来容忍 | 允许晚于当前至多 5 分钟 | 我的决定 |
