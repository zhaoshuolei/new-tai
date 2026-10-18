# 自定义数据管理设计

日期：2026-10-18

状态：已批准（2026-10-18），进入实现计划

## 1. Intent

为运营提供一个可配置的数据明细管理能力：运营为不同业务创建彼此独立的数据表，定义自己的字段和静态枚举，通过 API 或 Excel 写入数据，并按日期修正历史数据。商务只查看与自己客户相关的数据，运营查看全部数据。

首要用途是让运营及时通过明细发现业务问题，其次为未来与上下游客户对账保留稳定、可追溯的数据基础。

当前仓库是 FastAPI + MySQL 后端。本文的交付边界包括数据模型、迁移、API、导入任务、权限、菜单种子、审计和测试；管理页面在前端仓库实现。

## 2. Confirmed Decisions

| 主题 | 结论 |
| --- | --- |
| 表的数量 | 运营可创建任意多张彼此独立的数据表 |
| 字段自定义 | 支持文本、整数、小数、日期、布尔、静态枚举 |
| 固定客户列 | 每张表必须有上游客户列和下游客户列，二者均必填 |
| 客户行基数 | 每行最多一个上游客户和一个下游客户 |
| 客户方向 | 上游为我方收钱，下游为我方付钱；同一客户可在不同行承担不同方向 |
| 固定金额列 | 每张表必须有一个收入列和一个成本列，二者均必填，统一以人民币计 |
| 业务日期 | 每张表固定一个业务日期列，每行必填，一个上传可包含多天 |
| 结构变更 | 首次成功写入前可修改全部结构；首次成功写入后完全冻结 |
| Excel 修正语义 | 按业务日期整批快照替换；文件中出现的每个日期替换当前快照，未出现的日期不变 |
| 写入原子性 | 一个上传中的全部日期要么一起生效，否则全部不生效 |
| API 输入 | 支持结构化批量 JSON，与 Excel 使用同一套校验和替换服务 |
| 数据量 | 单次导入上限 100,000 行；采用后台任务处理 |
| 客户名称 | 未删除客户之间名称唯一；已删除客户不占用名称 |
| Excel 客户值 | 使用唯一客户名称；服务端解析为客户 ID；不再做人工同名确认 |
| 表访问 | 有数据表查看权限的人都能看到所有表定义 |
| 行访问 | 全局数据范围查看全部；其他数据范围只要任一上下游客户可见，就返回整行 |
| 异常发现 | 首版由运营通过筛选、排序、汇总自行判断；不做自动阈值、标红或告警 |
| 表结构演进 | 有数据后不原地修改；需要新结构时新建表 |

## 3. Goals

- 运营可在无数据时创建、编辑和删除自己的表结构。
- 每张表固定包含业务日期、上游客户、下游客户、收入、成本五个系统列。
- 运营可增加自定义字段，并配置必填、顺序、枚举选项。
- Excel 与 API 写入同一业务表时使用完全相同的类型转换、校验、权限和替换规则。
- 单次最多 100,000 行导入可异步执行，并可查看进度、失败原因和结果。
- 防止部分写入：任何一行或一个日期校验失败，整个导入都不生效。
- 保留每次成功替换的历史快照，支持对账追溯。
- 对有客户数据范围的用户实施行级过滤，且任一绑定客户可见时返回整行。
- 查询当前有效数据，并支持按日期、上下游客户、固定金额、固定字段和自定义字段筛选、排序与汇总。
- 将客户名称改为“仅活跃客户唯一”，为 Excel 提供无歧义的客户解析。

## 4. Non-goals

- 自动异常检测、阈值规则、告警通知和异常标记。
- 手工逐行新增、编辑或删除数据；本期写入只通过 Excel 或 API 批量导入。
- 公式字段、跨表关联、关联到非客户实体的动态字段、附件字段、多选枚举。
- 表内结构版本化或在线类型迁移；有数据后通过新建表完成演进。
- 跨表 Join、透视表、自定义 SQL、图表和报表设计器。
- 数据表按创建人或部门隔离；表定义对所有具有查看权限的人可见。
- 行级字段脱敏。商务看到一行时可看到另一侧客户及全部金额，这是已确认规则。
- CSV、旧版 `.xls`、宏工作簿或包含公式的 Excel 导入。
- 历史快照的自动清理、归档或物理删除。
- 本期不提供查询或导出已被替换历史快照的 API；快照仍完整保留，供后续对账版本功能使用。
- 本仓库中的前端页面；前端只消费本文定义的 API。
- 自动修复现有重复客户名称；迁移若发现活跃客户重名将失败，需先由业务清理。

## 5. Anti-goals

- 不允许由客户名称文本代替客户外键。
- 不允许 API 与 Excel 各自实现一套字段校验。
- 不允许先覆盖部分日期后才发现后续日期有错误。
- 不允许通过数据管理绕过客户数据范围，把未授权客户的行计入列表或汇总。
- 不允许在有数据后原地改变列类型、列语义或枚举值，从而改变历史数据的含义。
- 不允许把上传原文、客户名称、金额或自定义字段值写入审计日志或普通应用日志。
- 不允许直接以客户端文件名作为存储路径，也不允许信任客户端 MIME。
- 不允许运营自建的表产生任意数据库 DDL。
- 不允许把 Excel 公式当作可信的计算结果导入。

## 6. Domain Model

### 6.1 数据表定义 `custom_data_table`

- `id`
- `name`：显示名称，必填，去除首尾空白，最长 100，所有数据表之间唯一。
- `description`：可空，最长 500。
- `status`：`DRAFT` / `LOCKED`。
- `locked_at`：首次成功导入生效时写入。
- `created_by`、`updated_by`
- `created_at`、`updated_at`

状态规则：

- `DRAFT`：没有任何成功生效的导入，允许完整编辑和物理删除。
- `LOCKED`：至少有一次成功导入生效，结构和元数据完全冻结。
- `LOCKED` 表是未来对账依据，不允许删除。
- 新建表从一个完整的 DRAFT 定义开始，不直接复制已有表；需要新结构时手工新建。

### 6.2 字段定义 `custom_data_field`

- `id`：BIGINT，API 中使用其十进制字符串作为稳定字段键。
- `table_id`
- `label`：Excel 表头及 UI 显示名称，必填，最长 64，同一表内唯一。
- `field_type`：`TEXT` / `INTEGER` / `DECIMAL` / `DATE` / `BOOLEAN` / `ENUM` / `CUSTOMER`。`CUSTOMER` 仅供固定上下游系统字段使用，不开放为普通自定义字段类型。
- `system_key`：`BUSINESS_DATE` / `UPSTREAM_CUSTOMER` / `DOWNSTREAM_CUSTOMER` / `REVENUE` / `COST` / `CUSTOM`。
- `required`：系统字段恒为 true；自定义字段可配置。
- `sort_order`
- `created_at`、`updated_at`

约束：

- 每张表恰好包含五个系统字段。
- `BUSINESS_DATE` 必须为 `DATE`；上下游客户必须为系统客户引用；收入和成本必须为 `DECIMAL`。
- `(table_id, system_key)` 对非 `CUSTOM` 值唯一。
- 同一表内的自定义字段标签和系统字段标签一起参与唯一性校验。
- 字段标签去除首尾空白，空标签拒绝。
- 自定义字段数量上限 100。
- 系统字段的显示标签可在 DRAFT 阶段修改，但系统角色不可修改或删除。
- 系统字段可以在 DRAFT 阶段调整展示顺序。
- 自定义字段允许 `required`；固定字段不可设为可空。

### 6.3 枚举选项 `custom_data_enum_option`

- `id`
- `field_id`
- `label`：必填，去除首尾空白，最长 100，同一字段内唯一。
- `sort_order`
- `created_at`、`updated_at`

规则：

- 只有 `ENUM` 字段可以拥有选项。
- `ENUM` 字段至少有一个选项。
- 选项只能通过字段定义整体创建或修改；DRAFT 冻结后不可增删改。
- 数据库存储选项 ID，Excel 和 API 输入选项标签；返回时同时给标签。
- 空选项、重复选项或超过 1,000 项拒绝。

### 6.4 导入任务 `custom_data_import`

- `id`
- `table_id`
- `source`：`EXCEL` / `API`
- `status`：`QUEUED` / `VALIDATING` / `APPLYING` / `SUCCEEDED` / `FAILED`。
- `original_filename`：仅 Excel，有值，用于展示和审计，不用于存储路径。
- `storage_path`：私有原始文件路径；API 导入也保存规范化请求体文件。
- `content_sha256`
- `size_bytes`
- `requested_by`
- `total_rows`
- `valid_rows`
- `invalid_rows`
- `date_from`、`date_to`
- `error_count`
- `progress_percent`
- `failure_code`、`failure_message`
- `attempt_count`
- `lease_owner`、`lease_expires_at`、`heartbeat_at`
- `queued_at`、`started_at`、`finished_at`
- `created_at`、`updated_at`

规则：

- 单表同一时刻只允许一个未完成任务；创建任务和 worker 认领时都锁定表行，避免并发导入互相覆盖。
- 存在 `QUEUED`、`VALIDATING` 或 `APPLYING` 任务时，表定义修改和删除一律拒绝，避免任务按过期定义写入。
- 请求接受后返回 202 和任务 ID，不等待 100,000 行处理完成。
- worker 通过 MySQL 短事务和 `SELECT ... FOR UPDATE SKIP LOCKED` 认领任务，并写入租约；租约超时后可被其他 worker 安全恢复。实现不得假设只有一个 Web 进程。
- 任务状态只能按定义顺序前进；`SUCCEEDED` 和 `FAILED` 为终态。
- 相同原始文件不会自动视为幂等请求；每次导入都有独立任务和快照版本。

### 6.5 导入错误 `custom_data_import_error`

- `id`
- `import_id`
- `sheet_name`
- `row_number`
- `column_label`
- `field_id`：可空。
- `error_code`
- `message`

规则：

- 明细最多保存前 10,000 条错误，`error_count` 保存真实总数。
- 错误消息不得包含整行值；只允许包含字段标签、客户名称等定位信息和简短原因。
- 任一错误都会使整个任务失败。

### 6.6 快照 `custom_data_snapshot`

- `id`
- `table_id`
- `business_date`
- `import_id`
- `version`：同一表同一日期的递增版本。
- `status`：`STAGED` / `ACTIVE` / `SUPERSEDED`。
- `row_count`
- `created_at`
- `activated_at`

约束：

- `(table_id, business_date, version)` 唯一。
- 同一表同一日期最多一个 `ACTIVE` 快照。
- 旧快照永久保留，不物理删除。
- 只有成功任务的快照才能进入 `ACTIVE`。

### 6.7 当前快照指针 `custom_data_current_snapshot`

- `table_id`
- `business_date`
- `snapshot_id`
- `updated_at`

主键为 `(table_id, business_date)`。查询只从这里找到当前快照，不从业务日期推断最大版本。激活多个日期时，在同一短事务内切换所有指针。

### 6.8 数据记录 `custom_data_record`

- `id`
- `snapshot_id`
- `table_id`
- `business_date`：冗余存储，用于索引和查询。
- `upstream_customer_id`：外键至 `customer.id`，必填。
- `downstream_customer_id`：外键至 `customer.id`，必填。
- `upstream_customer_name_source`：导入时客户名称快照，必填。
- `downstream_customer_name_source`：导入时客户名称快照，必填。
- `revenue`：人民币金额，`DECIMAL(30,8)`，必填，可为负数。
- `cost`：人民币金额，`DECIMAL(30,8)`，必填，可为负数。
- `created_at`

规则：

- 客户可同时作为同一行的上下游；本期不禁止。
- 金额允许负数，以支持冲减、退款或调整；首版不存储币种，收入和成本固定按人民币解释，不做汇率换算。
- 客户名称快照只用于展示来源证据；当前客户关系始终以客户 ID 为准。
- 查询返回人民币毛利润 `revenue - cost`；当收入不为 0 时同时返回毛利率。
- 数据记录属于不可变快照，不原地修改。
- 记录 ID 只在其所属快照内稳定；重新导入同一日期后当前记录会获得新的 ID。若外部系统需要跨版本识别同一条业务明细，必须用自己的业务标识字段，不能依赖本系统的记录 ID。

### 6.9 自定义字段值 `custom_data_record_value`

- `id`
- `record_id`
- `field_id`
- `text_value`：`VARCHAR(4000)`，可空。
- `integer_value`：`BIGINT`，可空。
- `decimal_value`：`DECIMAL(30,8)`，可空。
- `date_value`：`DATE`，可空。
- `boolean_value`：`BOOLEAN`，可空。
- `enum_option_id`：外键至 `custom_data_enum_option.id`，可空。
- `created_at`

约束：

- `(record_id, field_id)` 唯一。
- 每个值只填充与字段类型对应的一个列。
- 非必填字段无值时可以不创建值行；必填字段必须有值行。
- 应用层验证 `field_id` 属于记录所属表，且 `enum_option_id` 属于该字段。
- 查询自定义字段时只对当前页记录加载值，避免 N+1。

## 7. 客户名称唯一

### 7.1 数据库约束

在 `customer` 表增加：

- `active_name`：生成列，`status = 'ACTIVE'` 时为去除首尾空白的 `name`，否则为 NULL。
- 唯一索引 `(active_name)`。

MySQL 允许多个 NULL，因此已删除客户不占用名称。应用层在所有新写入和更新前去除客户名称首尾空白；存量名称不强制回填，生成列仍用 `TRIM(name)` 计算唯一标识，既有展示名称保持不变。

名称比较沿用数据库列的 `utf8mb4_0900_ai_ci` 排序规则，因此大小写、重音差异视为同一名称。Excel 输入先去首尾空白，再用同一排序规则匹配，不承诺逐字节区分。

### 7.2 受影响流程

以下流程必须统一校验并处理唯一冲突：

- 手工创建客户。
- 天眼查回填创建客户。
- 修改客户名称。
- 恢复客户。
- Excel 和 API 的客户名称解析。

新增错误码 `7022 CUSTOMER_NAME_EXISTS`，HTTP 409。并发冲突由数据库唯一索引兜底并转换为 7022。

恢复已删除客户时，如果活跃客户已使用同名，恢复失败。迁移脚本必须先检查现有的活跃重名；存在重名时阻止迁移并不自动合并或改名。迁移错误只输出重复组数量和修复命令，不输出客户名称。

运维可显式运行只读脚本 `scripts/check_duplicate_customer_names.py`，脚本只在操作者当前终端或权限为 `0600` 的私有报告文件中输出重复组，供业务清理；该输出不进入应用日志、审计日志或任务错误。

## 8. Schema Lifecycle

### 8.1 DRAFT 阶段

- 可以新增、删除、重排字段。
- 可以修改字段标签、类型、必填状态、枚举选项、收入/成本及客户角色之外的系统字段标签。
- 五个系统字段的角色固定，不能增加第二个或删除。
- 可以修改表名称和描述。
- 存在未完成导入任务时，不能修改或删除表定义。
- 没有成功生效导入且没有未完成任务时可以删除表，删除采用物理删除，仅允许删除 DRAFT 完整定义。
- 删除 DRAFT 表时，在同一事务中级联删除其失败导入、错误、STAGED 快照和记录；事务提交后尽力删除对应私有源文件。审计记录保留，但不再保留失败导入业务明细。

### 8.2 冻结

- 第一次导入成功切换任一日期快照后，在同一事务中把表状态改为 `LOCKED` 并写 `locked_at`。
- 冻结后拒绝任何表或字段定义修改，包括名称、描述、字段、必填、顺序、枚举值和类型。
- 冻结后不接受手工新增/删除字段；API 返回 9003。
- 失败或仅排队/校验中的导入不会冻结表。
- 业务流程需要新结构时新建一张表。

## 9. Import Contract

Excel 和 JSON 都要求调用者拥有 `data:import`。除全局数据范围角色外，导入的上下游两个客户都必须落在调用者当前客户数据范围内；任一侧不可见时整批校验失败。该规则防止有限范围用户通过构造“另一侧不可见”的行绕过读取权限。

### 9.1 JSON API

```json
{
  "rows": [
    {
      "business_date": "2026-10-18",
      "upstream_customer_id": 101,
      "downstream_customer_id": 202,
      "revenue": "1942.51",
      "cost": "1155.14",
      "custom_values": {
        "37": "聚合流量",
        "42": 90105782,
        "45": "激励视频"
      }
    }
  ]
}
```

规则：

- 固定键为 `business_date`、`upstream_customer_id`、`downstream_customer_id`、`revenue`、`cost`。
- `custom_values` 的键为字段 ID 字符串；不得包含系统字段。
- 小数在 JSON 中使用字符串或 JSON number，服务端统一转换 `Decimal`；禁止经二进制浮点中转。
- 日期使用 `YYYY-MM-DD`。
- 布尔使用 JSON boolean。
- 枚举使用选项标签，服务端解析为选项 ID。
- 未知字段、系统字段重复、缺少必填字段或字段类型错误均使整批失败。
- 单次最多 100,000 行，请求压缩前最大 50 MiB。
- 客户 ID 必须对应未删除客户；API 不接受客户名称作为绑定值。
- 自定义文本字段最大 4,000 个 Unicode 字符；超长与 Excel 一样使整批失败，不截断。
- API 任务同样保存规范化原始请求用于追溯。

### 9.2 Excel

- 仅接受 `.xlsx`，单文件最大 50 MiB，最多 100,000 个数据行。
- 只读取一个可见工作表；隐藏工作表、图表页、宏文件和多工作表均拒绝。
- 第一行必须为字段标签。列顺序任意；缺失、重复或未知表头使整个文件失败。
- 每个业务日期可以出现多行；文件可以包含多个日期。
- 空行忽略；全空行不产生数据。
- 公式单元格拒绝，不读取缓存值。
- 文本按文本读取；以 `0` 开头的 ID 必须保持文本；超过 4,000 个 Unicode 字符使整批失败，不截断。
- 整数接受整数值或整数字符串，范围限制为有符号 64 位。
- 小数接受数值或十进制字符串，统一转 `Decimal`，最多 8 位小数。
- 日期接受 Excel 原生日期、`YYYY-MM-DD` 字符串和八位 `YYYYMMDD` 字符串/整数；拒绝时间或非法日期。
- 布尔接受原生布尔值、`TRUE`/`FALSE`、`是`/`否`、`1`/`0`。
- 静态枚举必须与配置的选项标签完全一致，去除首尾空白后匹配。
- 上下游客户名称去除首尾空白后，按数据库 `utf8mb4_0900_ai_ci` 规则匹配未删除客户；由于活跃名称已唯一，不再需要同名确认。
- 上传接口只创建任务；解析在线程池中执行，避免阻塞事件循环。

### 9.3 快照替换

1. 创建导入任务并锁定表，确保同一表无并发任务。
2. 解析并校验全部行，收集每行错误；任一行错误则任务失败且不创建可见数据。
3. 按业务日期生成新的 `STAGED` 快照和不可变记录。
4. 数据分批写入，避免单条 SQL 和单次 flush 过大；分块提交只写不可见的 STAGED 数据。
5. 全部日期准备完成后，在短事务中逐个日期切换 current pointer，并把新快照设为 `ACTIVE`、旧快照设为 `SUPERSEDED`。
6. 同一事务中把表从 `DRAFT` 改为 `LOCKED`，记录成功审计。
7. 任一步骤失败时任务为 `FAILED`；已写入的 STAGED 数据不可见，并由清理逻辑删除。

文件里出现哪些日期，就只替换哪些日期。未出现日期的当前快照保持不变。若一份文件包含多天，所有日期的指针必须一起切换，不允许部分日期成功。

## 10. Query and Visibility

### 10.1 表可见性

- `data:read` 可查看所有数据表定义和字段定义。
- 表列表对每名用户返回其可见的行数和日期范围；没有可见行的表仍显示，便于了解全局数据表。
- `DRAFT`、`LOCKED` 表均对读取者可见；仅 `DRAFT` 表允许具有写权限的人编辑。

### 10.2 行可见性

- 角色数据范围为 `ALL` 或超级管理员：不附加客户过滤。
- 其他数据范围：把当前用户的客户数据范围转换为可见客户 ID 集合，行满足以下任一条件即可见：
  - `upstream_customer_id` 在可见集合中。
  - `downstream_customer_id` 在可见集合中。
- 命中任一客户后返回整行，包括另一侧客户和全部金额。
- 行过滤必须在 count、列表和汇总之前应用；不能先算全局汇总再过滤。
- 导出能力不在本期范围；后续导出必须复用同一行可见性子查询。
- 已软删除客户不级联删除或隐藏历史数据，也不阻止客户删除。客户范围仍按原负责人计算，返回的客户信息包含当前名称、客户 ID 和删除状态；导入时仍只允许绑定未删除客户。
- 如果已删除客户的名称随后被新客户复用，历史数据仍通过原客户 ID 关联，不会自动改绑新客户；导入时名称快照用于识别人工证据。

### 10.3 运营判断

首版不提供自动异常判断。API 提供：

- 日期范围筛选。
- 上游和下游客户筛选。
- 收入、成本及毛利范围筛选。
- 自定义字段筛选。
- 文本包含、整数/小数/日期范围、布尔、枚举多选。
- 按固定字段或自定义字段升序/降序排序。
- 分页和总数。
- 当前筛选结果的行数、人民币收入合计、成本合计、毛利合计。

复杂跨字段 OR 条件、保存筛选器和异常规则均为非目标。

## 11. API Contract

统一前缀 `/api/v1`。

### 11.1 Data tables

- `GET /data-tables`：`data:read`，列出表定义、字段和当前可见行数摘要。
- `POST /data-tables`：`data:create`，一次提交表元数据和完整字段定义。
- `GET /data-tables/{table_id}`：`data:read`，返回表、字段、枚举和锁定状态。
- `PUT /data-tables/{table_id}`：`data:update`，仅 DRAFT 表可整体替换定义。
- `DELETE /data-tables/{table_id}`：`data:delete`，仅 DRAFT 表可删除。
- `GET /data-tables/{table_id}/template.xlsx`：`data:read`，按当前字段标签生成空模板。
- `POST /data-tables/{table_id}/records/search`：`data:read`，按第 10 节执行筛选、排序、分页和汇总。

创建表的请求示例：

```json
{
  "name": "客户投放日明细",
  "description": "按日记录上下游投放效果与收支",
  "fields": [
    {
      "label": "日期",
      "field_type": "DATE",
      "system_key": "BUSINESS_DATE"
    },
    {
      "label": "下游媒体",
      "field_type": "CUSTOMER",
      "system_key": "DOWNSTREAM_CUSTOMER"
    },
    {
      "label": "上游预算",
      "field_type": "CUSTOMER",
      "system_key": "UPSTREAM_CUSTOMER"
    },
    {
      "label": "收入",
      "field_type": "DECIMAL",
      "system_key": "REVENUE"
    },
    {
      "label": "成本",
      "field_type": "DECIMAL",
      "system_key": "COST"
    },
    {
      "label": "下游广告位Id",
      "field_type": "TEXT",
      "required": true,
      "system_key": "CUSTOM"
    },
    {
      "label": "曝光",
      "field_type": "INTEGER",
      "required": true,
      "system_key": "CUSTOM"
    }
  ]
}
```

`CUSTOMER` 只用于两个固定客户系统字段，不能由普通自定义字段使用。服务端根据 `system_key` 决定该字段表示上游还是下游客户。

### 11.2 Imports

- `POST /data-tables/{table_id}/imports/excel`：`data:import`，multipart 上传，返回 202 和任务。
- `POST /data-tables/{table_id}/imports/json`：`data:import`，提交 JSON 批量数据，返回 202 和任务。
- `GET /data-tables/{table_id}/imports`：`data:import`，分页查看导入历史。
- `GET /data-tables/{table_id}/imports/{import_id}`：`data:import`，返回状态、进度和汇总。
- `GET /data-tables/{table_id}/imports/{import_id}/errors`：`data:import`，分页查看错误明细。
- `GET /data-tables/{table_id}/imports/{import_id}/source`：`data:import`，下载原始文件；API 导入返回规范化的原始 JSON。

表内所有导入历史都只对该表有 `data:import` 权限的用户开放，但错误消息和原始数据不进入普通日志。

## 12. Permissions and Menu

新增功能权限：

- `data:read`
- `data:create`
- `data:update`
- `data:delete`
- `data:import`

菜单建议：

- 顶级菜单 `数据管理`，路径 `/data`，权限 `data:read`。
- 按钮 `创建数据表`，权限 `data:create`。
- 按钮 `编辑数据表`，权限 `data:update`。
- 按钮 `删除数据表`，权限 `data:delete`。
- 按钮 `导入数据`，权限 `data:import`。

运营、商务等业务角色不是硬编码名称。运营角色配置 `DataScope.ALL` 并拥有相应权限；商务角色拥有 `data:read`，其客户数据范围沿用现有角色配置。权限和范围必须同时校验，只有 `data:read` 不能让用户绕过商务范围。

## 13. Audit and Logging

审计 action：

- `data_table:create`
- `data_table:update`
- `data_table:delete`
- `data_import:request`
- `data_import:complete`
- `data_import:fail`

审计 resource type：

- `custom_data_table`
- `custom_data_import`

现有 `audit_log` 没有承载业务摘要的扩展字段，因此审计只记录对象、动作和结果，不修改审计表结构。导入来源、行数、日期范围、快照 ID 和错误总数保存在 `custom_data_import` 业务记录中，通过导入 ID 与审计关联。

审计和应用日志禁止记录：

- 客户名称和客户 ID 对应的业务内容。
- 收入和成本值。
- 自定义字段值。
- Excel 原始单元格内容。
- 原始文件的下载地址或可被外部直接访问的 URL。

原始导入文件存储在私有目录，不挂载到静态媒体路径。文件路径包含随机 UUID，客户端文件名只作为元数据显示。

## 14. Error Codes

使用新段 `9001` 起：

| Code | HTTP | 含义 |
| --- | --- | --- |
| 9001 | 404 | 数据表不存在 |
| 9002 | 409 | 数据表名称重复 |
| 9003 | 409 | 数据表已冻结，不能修改 |
| 9004 | 422 | 数据表或字段定义不合法 |
| 9005 | 404 | 导入任务不存在 |
| 9006 | 422 | 导入文件或请求格式不合法 |
| 9007 | 422 | 导入校验失败 |
| 9008 | 409 | 数据表已有进行中的导入 |
| 9009 | 422 | 字段筛选或排序不合法 |

客户重名使用 `7022 CUSTOMER_NAME_EXISTS`。字段值错误使用 9007，并在错误明细中给出定位；请求结构本身不合法使用 9006。

## 15. Storage and Processing Constraints

容量与性能验收基线：

- 最多 100 张表；单表可保留最多 100 个自定义字段。
- 单次导入最多 100,000 行，后台在 15 分钟内完成（测试环境基准需记录实际耗时）。
- 单表当前快照规模按 1,000,000 行、20 个自定义字段进行容量测试；历史快照不参与当前列表查询。
- 在 30 天日期范围并至少指定一个客户或一个高选择性字段时，分页列表 P95 不超过 3 秒，汇总 P95 不超过 5 秒。未加日期或客户条件的全表查询允许更慢，但不得阻塞其他请求。
- 超过上述容量不保证性能，需要基于真实慢查询重新评估索引或汇总结构。

- 新增运行依赖 `openpyxl`，用于只读解析 `.xlsx` 和生成模板。
- 新增运行依赖 `python-multipart`，用于 FastAPI 接收 Excel multipart 上传；不得信任客户端提供的文件名或 MIME，文件还必须校验扩展名和 XLSX 容器结构。
- 新增私有目录配置 `DATA_IMPORT_ROOT` 与 `DATA_IMPORT_MAX_BYTES=52428800`；目录不能与当前工作目录、媒体目录或跟进附件目录重叠。
- 上传完成后计算 SHA-256、保存原文件并创建任务，不把文件内容放入 Redis 或数据库大字段。
- 后台执行使用数据库任务和租约作为事实来源，提供独立 worker 进程 `python -m app.workers.data_import`；API 进程只入队，不依赖仅存在内存中的后台任务。
- worker 可运行多个实例，通过 `SELECT ... FOR UPDATE SKIP LOCKED` 认领任务并使用带过期时间的租约；进程重启或租约过期后恢复可重试任务。
- 同一表任务串行；不同表可并行，但并发数受全局 worker 上限控制。
- Excel 解析在线程池运行；异步数据库写入按固定批次 flush，例如每 1,000 行。
- API 与 worker 必须共享数据库和共享导入文件目录；如果使用对象存储，文件接口必须支持 worker 和 API 共同访问。
- `STAGED` 快照和失败任务文件由独立清理流程处理。清理不能删除 `ACTIVE` 或 `SUPERSEDED` 快照，也不能删除仍需重试任务的文件。

## 16. Testing Strategy

### 16.1 Customer uniqueness

- 活跃客户名称重名返回 7022。
- 已删除客户名称可以被新客户使用。
- 恢复同名客户返回 7022。
- 手工创建、天眼查创建、更新、恢复四条路径一致。
- 并发创建同名客户由唯一索引兜底。
- 首尾空白、大小写和数据库排序规则等价名称按同一客户名称处理。
- 迁移前发现活跃重名时失败，错误不含敏感业务数据。
- 重名检查脚本支持终端和私有报告输出，不写入应用或审计日志。

### 16.2 Definitions

- 每张表必须且只能有一个业务日期、上下游客户、收入和成本系统字段。
- 自定义字段类型、必填、标签、枚举和数量边界。
- 无数据时可修改和删除。
- 首次成功导入后所有定义修改被拒绝。
- 失败导入不冻结表。
- 有数据表不可删除。

### 16.3 Import and snapshot

- Excel 和 JSON 对相同数据产生相同记录。
- 单日替换后旧快照变为 `SUPERSEDED`，新快照为 `ACTIVE`。
- 多日期替换要么全部生效要么全部不生效。
- 未出现在上传中的日期保持原快照。
- 同一日期的重复导入生成递增版本。
- 100,000 行以内可完成并报告进度。
- 错误行仅保存最多 10,000 条明细但统计全部错误。
- 任一非法客户、金额、日期、枚举、公式或超过 4,000 字符的文本导致整批失败。
- 有限数据范围的导入者试图绑定任一不可见客户时，整批失败。
- 并发导入同一张表只允许一个任务。
- 多 worker 竞争时每个任务只被一个租约持有者执行。
- 进程重启或租约过期后任务可恢复，已激活快照不会重复激活。

### 16.4 Query and authorization

- 全局范围用户看到所有行与汇总。
- 商务能看到上游客户属于自己的整行，包括另一侧客户。
- 商务能看到下游客户属于自己的整行。
- 两边客户都不可见的行不出现在列表、总数和汇总中。
- 客户改名后返回当前客户名称，同时保留导入时名称快照。
- 已删除客户对应的历史数据仍可查询，不因名称被新客户复用而改绑。
- 日期、客户、金额、自定义字段筛选和排序。
- 枚举筛选使用选项 ID，显示使用标签。
- 分页稳定，汇总不受分页影响。
- 毛利和毛利率计算正确。

### 16.5 Security and operations

- 非 xlsx、损坏文件、超限文件、隐藏表、多表和公式单元格被拒绝。
- 客户端文件名不能造成路径穿越。
- 审计和普通日志不包含原始值。
- 权限缺失时 403，客户范围外不能通过猜 ID 读取或导入行。
- 数据库迁移可在空库上升级与回滚；客户唯一索引在 MySQL 实际执行。
- 100,000 行导入和 1,000,000 行当前快照的基准达到第 15 节性能门槛，并记录索引执行计划。

## 17. Migration

1. 为 `customer.active_name` 增加生成列和唯一索引。
2. 新增 `custom_data_table`、`custom_data_field`、`custom_data_enum_option`。
3. 新增 `custom_data_import`、`custom_data_import_error`。
4. 新增 `custom_data_snapshot`、`custom_data_current_snapshot`、`custom_data_record`、`custom_data_record_value`。
5. 增加所需外键和索引：
   - current pointer 的主键。
   - record 的 `(snapshot_id)`、`(table_id, business_date)`、上下游客户索引。
   - value 的 `(record_id)`、`(field_id)` 以及各 typed value 与 record 的组合索引。
6. 更新 `init_data` 加入权限、菜单和按钮。
7. 增加导入私有目录配置。
8. 迁移不写入任何业务数据，也不自动修复重名客户。

## 18. Risks and Tradeoffs

- EAV 存储避免了任意 DDL，但多字段筛选与排序需要动态 join。应限制字段数并优先用日期、客户和固定金额缩小候选集。
- 快照不可变会持续增加存储量，但对账追溯需要保留，首版不做清理。
- 100,000 行的异步导入需要可靠任务恢复和多批次写入，不能依赖一次性大事务或仅内存中的 `BackgroundTasks`。
- “任一客户可见即可看到整行”是已确认业务规则，但从客户隐私角度存在跨客户信息可见风险，必须通过权限测试固定行为。
- 客户名称唯一会成为全系统约束，存量重名必须先清理，迁移可能需要在业务低峰执行。
- 多日期上传的统一生效通过 pointer 切换保证，但实现必须防止激活前行泄漏到查询结果。

## 19. Open Decisions for the Builder

以下不影响产品范围的实现细节由构建者决定：

- STAGED 快照清理任务的具体调度方式。
- 导入 worker 的批量大小、并发上限和重启恢复退避策略。
- 文本筛选是否只支持包含/精确，还是增加大小写和空白归一化选项。
- 模板和原始文件下载的 Content-Disposition 文件名格式。
- 数据库索引的最终组合方式，以真实 100,000 行基准测试为准。

## 20. Approval Gate

本设计已于 2026-10-18 获用户批准。下一步是实现计划；实现阶段必须保持本文确认的产品边界，不得在计划中弱化客户数据范围、结构冻结或快照原子性。
