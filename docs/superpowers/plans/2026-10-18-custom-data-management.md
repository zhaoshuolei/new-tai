# 自定义数据管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a backend-only configurable data management subsystem with independent table definitions, fixed customer/revenue/cost fields, Excel and JSON snapshot imports, row-level customer scoping, and queryable current data.

**Architecture:** Definitions and immutable typed values use metadata tables plus EAV value rows to avoid user-driven DDL. Imports are persisted as private source files and durable jobs; a separately launched DB-claiming worker validates rows, stages per-date snapshots, then atomically switches all affected date pointers. Query APIs read only current snapshots and apply customer scope before count, rows, and summaries.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, MySQL 8.4, Alembic, pytest/pytest-asyncio, openpyxl, python-multipart.

**Spec:** `.scratch/data-management/spec.md`（已批准）。执行者必须同时阅读 spec，不得弱化客户范围、结构冻结或快照原子性。

## Global Constraints

- 当前 Alembic head 是 `c375c8de4899`；Task 1 的迁移接续该 revision，Task 2 的迁移接续 Task 1 生成的 revision。
- 新权限固定为 `data:read`、`data:create`、`data:update`、`data:delete`、`data:import`；业务角色名称不得硬编码。
- 每张表恰好有一个 `BUSINESS_DATE`、一个 `UPSTREAM_CUSTOMER`、一个 `DOWNSTREAM_CUSTOMER`、一个 `REVENUE`、一个 `COST` 系统字段；固定字段全部必填。
- 自定义字段类型仅允许 `TEXT`、`INTEGER`、`DECIMAL`、`DATE`、`BOOLEAN`、`ENUM`；`CUSTOMER` 只供固定客户字段使用。
- 每行上下游客户均必填，收入与成本均必填并按人民币解释；每类金额最多一个字段。
- 首次成功导入生效后，表名称、描述、字段、必填、顺序、类型、枚举全部冻结；失败导入不冻结。
- Excel/JSON 单次最多 100,000 行；Excel 最大 50 MiB；文本值最大 4,000 个 Unicode 字符，超限拒绝且不截断。
- 多日期导入必须全部成功或全部不生效；文件未包含的日期不得变化。
- 客户名称仅在未删除客户之间唯一；删除后释放名称；Excel 名称比较按去空白后的 MySQL `utf8mb4_0900_ai_ci` 语义。
- 行可见性：`DataScope.ALL`/超级管理员看全部；其他范围只在任一上下游客户可见时返回整行。
- 有限范围导入者必须同时能看到上下游两个客户，否则整批失败。
- 导入错误和审计不得记录字段值、客户名称、金额或原始单元格内容。
- 分页上限沿用 `MAX_PAGE_SIZE = 100`；错误响应使用统一 envelope 和 `BusinessError`。
- 代码标识符和表名用英文；注释、错误消息和文档用中文或项目既有英文风格；ruff line-length 88，mypy strict。
- 测试命令统一使用 `.venv/bin/pytest`；涉及 Alembic 时设置 `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x`。
- 每个任务结束提交一次；如果用户在执行时明确要求不提交，则用该任务的测试通过结果代替提交步骤。

## Review Focus

下面五类风险在 spec 中没有完整样例，必须在对应任务测试中固定：

1. **多日期原子替换与恢复**：任一日期失败、进程崩溃或重复领取都不得暴露半批新数据，也不得重复激活（Task 8、Task 9）。
2. **跨客户行权限**：任一绑定客户可见即可读整行；有限范围用户不能通过导入另一侧不可见客户反向获得该行（Task 9、Task 11、Task 12）。
3. **定义冻结与并发导入**：排队或执行中的导入不能与结构修改并发；首次成功写入后所有定义修改失败（Task 3、Task 9）。
4. **Excel 类型保真**：前导零文本、八位日期、Excel 原生日期、布尔、Decimal、公式、隐藏工作表、超长文本不能产生静默错误（Task 7）。
5. **客户名称唯一与历史绑定**：软删除客户释放名称、恢复冲突、迁移重名阻断、历史行不因名称复用改绑（Task 1、Task 11）。

---

## File Structure

- Create `app/models/data_management.py`：九张数据管理模型及枚举。
- Modify `app/models/customer.py`、`app/models/__init__.py`。
- Modify `app/core/errors.py`、`app/core/config.py`。
- Create `migrations/versions/<rev>_customer_active_name.py`、`migrations/versions/<rev>_custom_data_management.py`。
- Create `app/repositories/data_table.py`、`data_import.py`、`data_query.py`。
- Create `app/services/customer_name.py`、`data_table.py`、`data_import_storage.py`、`data_import_parser.py`、`data_import.py`、`data_query.py`。
- Create `app/schemas/data_table.py`、`data_import.py`、`data_query.py`。
- Create `app/api/data_tables.py`、`app/api/data_imports.py`。
- Create `app/workers/__init__.py`、`app/workers/data_import.py`。
- Modify `app/api/router.py`、`app/main.py`、`scripts/init_data.py`。
- Create `scripts/check_duplicate_customer_names.py`、`scripts/benchmark_data_management.py`。
- Modify `pyproject.toml`、`uv.lock`、`.env.example`、`.gitignore`。
- Create focused tests under `tests/test_data_*.py` and modify customer/config/seed tests.

---

### Task 1: Enforce Active Customer Name Uniqueness

**Files:**
- Modify: `app/core/errors.py`
- Modify: `app/models/customer.py`
- Modify: `app/models/__init__.py`
- Modify: `app/repositories/customer.py`
- Modify: `app/services/customer.py`
- Create: `app/services/customer_name.py`
- Create: `migrations/versions/<rev>_customer_active_name.py`
- Create: `scripts/check_duplicate_customer_names.py`
- Test: `tests/test_customer_name_unique.py`
- Test: `tests/test_customer_service.py`
- Test: `tests/test_customer_soft_delete.py`

**Interfaces:**
- Produces: `ErrorCode.CUSTOMER_NAME_EXISTS = 7022`.
- Produces: `normalize_customer_name(value: str) -> str`; returns the trimmed name and raises `BusinessError(7003, 422)` when the result is empty.
- Produces: `customer_name_lookup_key(value: str) -> str`; applies Unicode NFKC, removes combining marks, strips, and casefolds so in-process Excel lookup follows the same case/accent-insensitive intent as `utf8mb4_0900_ai_ci`.
- Produces: `Customer.active_name: str | None`, a persisted generated column `CASE WHEN status = 'ACTIVE' THEN TRIM(name) END`, with a unique index.
- Produces: `app.repositories.customer.get_active_by_name(session, name: str, *, for_update: bool = False) -> Customer | None`.
- All create/update/Tianyancha/restore paths store trimmed names and map the active-name unique conflict to 7022.

- [ ] **Step 1: Write failing uniqueness tests**

Add tests that create two active customers with the same trimmed name and assert the second raises 7022; create `" 客户甲 "` and assert stored `name == "客户甲"`; delete the first and create the same name successfully; restore the deleted row while the replacement is active and assert 7022.

```python
async def test_active_customer_name_is_unique(session, context):
    await customer_service.create_customer(
        session, CustomerCreate(name=" 客户甲 "), context=context
    )
    with pytest.raises(BusinessError) as exc:
        await customer_service.create_customer(
            session, CustomerCreate(name="客户甲"), context=context
        )
    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_customer_name_unique.py -v`

Expected: FAIL because `CUSTOMER_NAME_EXISTS` and `Customer.active_name` do not exist.

- [ ] **Step 3: Implement name normalization and all conflict paths**

Add `CUSTOMER_NAME_EXISTS = 7022`. Add the generated column and unique index to the model. Add `normalize_customer_name`. Use it in manual create, Tianyancha create, update, and restore. Add savepoint/IntegrityError recovery that re-reads the active customer by normalized name and raises 7022. Add the repository lookup with `TRIM(Customer.name) == name` and `Customer.status == ContactStatus.ACTIVE`.

Create the migration from `c375c8de4899`. Before DDL, execute a grouped query for duplicate active trimmed names under the column collation and raise `RuntimeError` if any group exists; include only the group count. Add the generated column and unique index.

Create `scripts/check_duplicate_customer_names.py`; it must be read-only, accept `--output PATH` for a mode-0600 private file, and print to stdout only when no output path is supplied.

- [ ] **Step 4: Verify migration and tests**

Run: `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head`

Expected: succeeds on a clean test DB; on a database with active duplicates, fails without printing names.

Run: `.venv/bin/pytest tests/test_customer_name_unique.py tests/test_customer_service.py tests/test_customer_soft_delete.py tests/test_customer_e2e.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/errors.py app/models/customer.py app/models/__init__.py app/repositories/customer.py app/services/customer.py app/services/customer_name.py migrations/versions scripts/check_duplicate_customer_names.py tests/test_customer_name_unique.py tests/test_customer_service.py tests/test_customer_soft_delete.py
git commit -m "feat: enforce unique active customer names"
```

---

### Task 2: Add Data Management Models and Migration

**Files:**
- Create: `app/models/data_management.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/<rev>_custom_data_management.py`
- Test: `tests/test_data_management_models.py`
- Test: `tests/test_data_management_migration.py`

**Interfaces:**
- Produces enums: `DataTableStatus`, `DataFieldType`, `DataFieldRole`, `DataImportSource`, `DataImportStatus`, `DataSnapshotStatus`.
- Produces models: `DataTableDefinition` (`custom_data_table`), `DataFieldDefinition` (`custom_data_field`), `DataEnumOption` (`custom_data_enum_option`), `DataImportJob` (`custom_data_import`), `DataImportError` (`custom_data_import_error`), `DataSnapshot` (`custom_data_snapshot`), `DataCurrentSnapshot` (`custom_data_current_snapshot`), `DataRecord` (`custom_data_record`), `DataRecordValue` (`custom_data_record_value`).
- `DataTableDefinition.name` is unique.
- `DataFieldDefinition` has `field_type`, `system_key`, `required`, `sort_order`, `system_key_unique` generated to NULL for `CUSTOM`, unique `(table_id, system_key_unique)`, and unique `(table_id, label)`.
- `DataEnumOption` has unique `(field_id, label)`.
- `DataImportJob` includes `source`, `status`, `original_filename`, `storage_path`, `content_sha256`, `size_bytes`, requested counts/date range/error count/progress/failure fields, `attempt_count`, lease fields, and lifecycle timestamps.
- `DataSnapshot` has unique `(table_id, business_date, version)` and status `STAGED|ACTIVE|SUPERSEDED`.
- `DataCurrentSnapshot` is a SQLAlchemy Core `Table` named `custom_data_current_snapshot` with no inherited `id`; its composite primary key is `(table_id, business_date)`.
- `DataRecord` stores `snapshot_id`, redundant `table_id`/`business_date`, required upstream/downstream customer FKs, source-name snapshots, and required `DECIMAL(30,8)` revenue/cost.
- `DataRecordValue` stores unique `(record_id, field_id)` and typed nullable columns for text, integer, decimal, date, boolean, enum option.
- All ORM models except `DataCurrentSnapshot` inherit `Base`; that pointer table uses explicit `table_id`, `business_date`, `snapshot_id`, and `updated_at` columns.

- [ ] **Step 1: Write failing model and migration tests**

Assert enum values, unique constraints, generated `system_key_unique`, required/FK columns, decimal precision/scale, snapshot version uniqueness, current-pointer PK, cascade directions, and record indexes.

```python
def test_field_system_key_is_unique_but_custom_is_repeatable() -> None:
    computed = DataFieldDefinition.__table__.c.system_key_unique.computed
    assert "CUSTOM" in str(computed.sqltext)
    unique_columns = {
        tuple(column.name for column in index.columns)
        for index in DataFieldDefinition.__table__.indexes
        if index.unique
    }
    assert ("table_id", "system_key_unique") in unique_columns
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_management_models.py -v`

Expected: FAIL because the model module does not exist.

- [ ] **Step 3: Implement models and migration**

Create the enums and models with explicit enum names. Use `ON DELETE CASCADE` only for table deletion cleanup and snapshot/record/error ownership; use `RESTRICT` for customer, field-definition, enum-option, active snapshot, and audit-relevant references where accidental deletion must fail. Add indexes from the spec, including record customer/date indexes and value typed-value indexes.

Generate the migration from Task 1's revision. Create tables in dependency order and reverse drops in `downgrade()`.

- [ ] **Step 4: Run migration and model tests**

Run: `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head && JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic downgrade -1 && JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head`

Expected: all three commands succeed.

Run: `.venv/bin/pytest tests/test_data_management_models.py tests/test_data_management_migration.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/data_management.py app/models/__init__.py migrations/versions tests/test_data_management_models.py tests/test_data_management_migration.py
git commit -m "feat: add data management persistence model"
```

---

### Task 3: Implement Data Table Definitions

**Files:**
- Create: `app/schemas/data_table.py`
- Create: `app/repositories/data_table.py`
- Create: `app/services/data_table.py`
- Modify: `app/core/errors.py`
- Test: `tests/test_data_table_service.py`
- Test: `tests/test_data_table_repository.py`

**Interfaces:**
- Produces schemas `DataEnumOptionInput(label, sort_order)`, `DataFieldInput(label, field_type, system_key, required=False, sort_order, options=[])`, `DataTableCreate(name, description, fields)`, `DataTableReplace`, `DataTableDefinitionRead`, `DataFieldRead`, `DataEnumOptionRead`.
- Produces `ResolvedDataDefinition(table, fields_by_id, fields_by_system_key, enum_options_by_id, enum_options_by_field_id)`.
- Produces repository functions `get_table`, `get_table_for_update`, `list_tables`, `load_resolved_definition`, `create_table`, `replace_table`, `delete_table`, `has_unfinished_import`.
- Produces service functions `create_table`, `get_table`, `replace_table`, `delete_table`, `list_tables`, each accepting `context: AuthContext`.
- Adds the complete error-code set in `app/core/errors.py`: `DATA_TABLE_NOT_FOUND = 9001`, `DATA_TABLE_NAME_EXISTS = 9002`, `DATA_TABLE_LOCKED = 9003`, `DATA_TABLE_INVALID = 9004`, `DATA_IMPORT_NOT_FOUND = 9005`, `DATA_IMPORT_INVALID = 9006`, `DATA_IMPORT_VALIDATION_FAILED = 9007`, `DATA_IMPORT_IN_PROGRESS = 9008`, `DATA_QUERY_INVALID = 9009`.

- [ ] **Step 1: Write failing definition tests**

Cover exactly five fixed roles and required types, duplicate labels/roles, enum option rules, 100-custom-field limit, table name conflict, full replacement while DRAFT, rejection after LOCKED, rejection while a QUEUED import exists, physical deletion only for an unused DRAFT table, and the exact values of error codes 9001 through 9009.

```python
async def test_locked_table_rejects_any_definition_change(session, context):
    table = await data_table_service.create_table(
        session, make_table_payload(), context=context
    )
    table.status = DataTableStatus.LOCKED
    table.locked_at = datetime.now(UTC)
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await data_table_service.replace_table(
            session, table.id, make_table_payload(name="新名称"), context=context
        )
    assert exc.value.code == ErrorCode.DATA_TABLE_LOCKED
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_table_service.py tests/test_data_table_repository.py -v`

Expected: FAIL because services and schemas do not exist.

- [ ] **Step 3: Implement validation and lifecycle**

Normalize table names and field labels with `strip`. Validate exactly one field for each fixed role, fixed role/type combinations, unique labels, enum options only on `ENUM`, at least one option and at most 1,000, custom fields at most 100. Create custom field IDs only in the DB.

Repository `replace_table` deletes and recreates field/option definitions only while DRAFT; IDs may change because no data can exist. Acquire the table row with `FOR UPDATE` before checking active imports, replacing, or deleting so an import cannot be enqueued concurrently. Translate duplicate table-name `IntegrityError` to 9002 in a savepoint.

- [ ] **Step 4: Run definition tests**

Run: `.venv/bin/pytest tests/test_data_table_service.py tests/test_data_table_repository.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/data_table.py app/repositories/data_table.py app/services/data_table.py app/core/errors.py tests/test_data_table_service.py tests/test_data_table_repository.py
git commit -m "feat: add configurable data table definitions"
```

---

### Task 4: Add Definition APIs and Seed Permissions

**Files:**
- Create: `app/api/data_tables.py`
- Modify: `app/api/router.py`
- Modify: `app/main.py`
- Modify: `scripts/init_data.py`
- Modify: `tests/test_seed_data.py`
- Test: `tests/test_data_table_api.py`
- Test: `tests/test_data_table_permissions.py`

**Interfaces:**
- Produces routes `GET /api/v1/data-tables`, `POST /api/v1/data-tables`, `GET /api/v1/data-tables/{table_id}`, `PUT /api/v1/data-tables/{table_id}`, `DELETE /api/v1/data-tables/{table_id}`.
- Permissions: list/detail `data:read`; create `data:create`; replace `data:update`; delete `data:delete`.
- Produces menu seeds: top-level `数据管理` (`/data`, `data:read`) and four child button permissions.
- Registers audit actions `data_table:create`, `data_table:update`, `data_table:delete` with resource type `custom_data_table`.

- [ ] **Step 1: Write failing API and seed tests**

Test response envelopes, 401/403, create/detail/update/delete, non-super-admin permissions not implicitly granted, and exact `EXPECTED_PERMISSION_CODES` additions.

```python
async def test_sales_can_read_definition_but_cannot_create(client, sales_headers):
    assert (
        await client.get("/api/v1/data-tables", headers=sales_headers)
    ).status_code == 200
    response = await client.post(
        "/api/v1/data-tables", json=make_table_payload(), headers=sales_headers
    )
    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.FORBIDDEN
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_table_api.py tests/test_data_table_permissions.py tests/test_seed_data.py -v`

Expected: FAIL because the router and permission seeds do not exist.

- [ ] **Step 3: Implement routes and seed data**

Add the router, route dependencies, response conversion, and audit registration. Add the five permission codes to the seed tree and role/menu initialization behavior used by existing features. Add `数据管理` to OpenAPI tags. Do not hardcode role names.

- [ ] **Step 4: Run API and seed tests**

Run: `.venv/bin/pytest tests/test_data_table_api.py tests/test_data_table_permissions.py tests/test_seed_data.py tests/test_app_boot.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/data_tables.py app/api/router.py app/main.py scripts/init_data.py tests/test_seed_data.py tests/test_data_table_api.py tests/test_data_table_permissions.py
git commit -m "feat: expose data table definition APIs"
```

---

### Task 5: Add Private Import Storage and Dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Modify: `.gitignore`
- Create: `app/services/data_import_storage.py`
- Test: `tests/test_data_import_storage.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `StoredImportSource(storage_path: str, original_name: str | None, size_bytes: int, content_sha256: str)`.
- Produces `save_import_bytes(content: bytes, *, suffix: Literal[".xlsx", ".json"], original_name: str | None = None) -> StoredImportSource`.
- Produces `read_import_source(storage_path: str) -> BinaryIO`.
- Produces `delete_import_source(storage_path: str) -> None`.
- Config: `data_import_root: str = "data-imports"`, `data_import_max_bytes: int = 52428800`.
- Config validation rejects empty/current/ancestor paths and overlap with media or follow-up attachment roots.
- Dependencies: add `openpyxl` and `python-multipart` through uv.

- [ ] **Step 1: Write failing storage/config tests**

Test successful hashing, distinct UUID filenames, exact maximum, one byte over, path traversal rejection, cleanup, and root-overlap validation.

```python
def test_import_storage_rejects_oversized_content(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_import_root", str(tmp_path))
    monkeypatch.setattr(settings, "data_import_max_bytes", 4)
    with pytest.raises(BusinessError) as exc:
        save_import_bytes(b"12345", suffix=".json")
    assert exc.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_import_storage.py tests/test_config.py -v`

Expected: FAIL because configuration and storage module do not exist.

- [ ] **Step 3: Add dependencies and implement bounded storage**

Run `uv add openpyxl python-multipart` from the repository root. Implement atomic temp-file write followed by replacement, SHA-256 accumulation, random storage names, and bounded reads. Never build a path from an original filename. Add config validators analogous to the existing follow-up attachment root validation.

- [ ] **Step 4: Verify storage and configuration**

Run: `.venv/bin/pytest tests/test_data_import_storage.py tests/test_config.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock app/core/config.py app/services/data_import_storage.py .env.example .gitignore tests/test_data_import_storage.py tests/test_config.py
git commit -m "feat: add private data import storage"
```

---

### Task 6: Implement Shared Row Validation and JSON Parsing

**Files:**
- Create: `app/schemas/data_import.py`
- Create: `app/services/data_import_parser.py`
- Test: `tests/test_data_import_parser_json.py`

**Interfaces:**
- Produces schemas `DataImportJsonRequest(rows: list[JsonImportRow])`, `JsonImportRow`, `DataImportRead`, `DataImportErrorRead`.
- Produces dataclasses `CustomerIdentity(id, name, status)`, `FieldValue`, `ValidatedImportRow`, `RowIssue`, `ParsedImportRow`.
- Produces `async load_customer_name_index(session, visible_customer_ids: set[int] | None = None) -> dict[str, CustomerIdentity]`; keys come from `customer_name_lookup_key`.
- Produces `iter_json_rows(path: Path, definition: ResolvedDataDefinition, customer_index: Mapping[str, CustomerIdentity], allowed_customer_ids: set[int] | None) -> Iterator[ParsedImportRow]`.
- Produces `validate_raw_row(raw: Mapping[str, object], definition: ResolvedDataDefinition, customer_index: Mapping[str, CustomerIdentity], allowed_customer_ids: set[int] | None) -> ValidatedImportRow | list[RowIssue]`.
- Produces `json_import_row_count(path: Path) -> int`.

- [ ] **Step 1: Write failing JSON validation tests**

Cover all supported types, decimal strings, signed 64-bit integers, booleans, ISO dates, enum labels, required system/custom fields, unknown fields, 4,000-character boundaries, invisible customers, deleted customers, and field-scoped errors.

```python
def test_json_decimal_never_uses_binary_float_and_accepts_negative_adjustment():
    result = validate_raw_row(
        valid_json_row(revenue="-1942.510000", cost="1155.140000"),
        definition,
        customer_index,
        allowed_customer_ids=None,
    )
    assert isinstance(result, ValidatedImportRow)
    assert result.revenue == Decimal("-1942.510000")
    assert result.cost == Decimal("1155.140000")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_import_parser_json.py -v`

Expected: FAIL because parser types and functions do not exist.

- [ ] **Step 3: Implement shared validation and JSON iterator**

Read the private JSON file with standard `json`, enforce one object with a `rows` list, stream rows from `json.load`, and yield either a validated row or row-scoped issues. Parse decimal input using `json.loads(..., parse_float=Decimal)` or an equivalent path that never passes through binary float. Use fixed `system_key` fields from the resolved definition. Customer names are resolved only for Excel; JSON customers are known IDs.

Do not leak raw values in issue messages. Unknown field IDs, non-custom system values in `custom_values`, duplicate required keys, and unparseable types are validation failures.

- [ ] **Step 4: Run JSON parser tests**

Run: `.venv/bin/pytest tests/test_data_import_parser_json.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/data_import.py app/services/data_import_parser.py tests/test_data_import_parser_json.py
git commit -m "feat: validate dynamic JSON data imports"
```

---

### Task 7: Implement Excel Parsing and Template Generation

**Files:**
- Modify: `app/services/data_import_parser.py`
- Test: `tests/test_data_import_parser_excel.py`

**Interfaces:**
- Produces `iter_excel_rows(path: Path, definition: ResolvedDataDefinition, customer_index: Mapping[str, CustomerIdentity], allowed_customer_ids: set[int] | None) -> Iterator[ParsedImportRow]`.
- Produces `build_excel_template(definition: ResolvedDataDefinition) -> bytes`.
- Accepts exactly one visible sheet; rejects hidden/multiple sheets, formulas, missing/duplicate/unknown headers, more than 100,000 data rows, and invalid XLSX signatures.
- Accepts dates as native Excel dates, `YYYY-MM-DD`, or eight-digit `YYYYMMDD`; booleans as native bool/TRUE/FALSE/是/否/1/0; integers and decimals as native numbers or strings.
- Customer cells resolve names through `customer_index[customer_name_lookup_key(raw_name)]`; both customer IDs must be active and allowed.
- Template writes one header row with the exact configured labels, no example data.

- [ ] **Step 1: Write failing Excel tests**

Generate workbooks in `tmp_path` with `openpyxl` and assert every supported type, leading-zero text preservation, `20261018` date parsing, native date parsing, customer matching, row/column error locations, formula rejection, hidden-sheet rejection, header mismatch rejection, and the 100,000-row guard.

```python
def test_excel_eight_digit_date_and_zero_prefixed_id(tmp_path):
    path = write_workbook(
        tmp_path,
        headers=definition.labels,
        rows=[excel_row(date_value=20261018, text_id="004047806")],
    )
    results = list(iter_excel_rows(path, definition, customer_index, None))
    assert results[0].row.business_date == date(2026, 10, 18)
    assert results[0].row.text_values[text_field.id] == "004047806"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_import_parser_excel.py -v`

Expected: FAIL because Excel parsing is not implemented.

- [ ] **Step 3: Implement read-only workbook parsing and template output**

Use `load_workbook(..., read_only=True, data_only=False)`, reject formula cells by inspecting each cell value/type, and iterate rows without using cell coordinates for storage. Map headers by normalized configured label and preserve order-independent matching. Parse workbook content in a worker thread; this module remains synchronous. Build template bytes with `Workbook` and a single visible sheet.

- [ ] **Step 4: Run Excel parser tests**

Run: `.venv/bin/pytest tests/test_data_import_parser_excel.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/data_import_parser.py tests/test_data_import_parser_excel.py
git commit -m "feat: parse and generate dynamic Excel data workbooks"
```

---

### Task 8: Implement Import Jobs, Leases, and Snapshot Persistence

**Files:**
- Create: `app/repositories/data_import.py`
- Create: `app/services/data_import.py`
- Test: `tests/test_data_import_repository.py`
- Test: `tests/test_data_snapshot_service.py`

**Interfaces:**
- Produces `create_import_job(...) -> DataImportJob`, `get_import_job(...) -> DataImportJob | None`, `list_import_jobs(...)`, `list_import_errors(...)`, and `list_current_snapshots(session, table_id: int) -> list[DataSnapshot]`.
- Produces `has_unfinished_import(session, table_id: int) -> bool`.
- Produces `claim_next_import_job(session, *, worker_id: str, lease_seconds: int = 300) -> DataImportJob | None`.
- Produces `renew_import_lease(session, *, import_id: int, worker_id: str, lease_seconds: int) -> bool`.
- Produces `start_staged_import(session, job: DataImportJob, business_dates: Collection[date]) -> dict[date, DataSnapshot]`, `append_staged_rows(session, snapshot_id: int, rows: Sequence[ValidatedImportRow])`, `activate_import(session, *, import_id: int, worker_id: str) -> None`, and `discard_staged_import(session, import_id: int)`.
- `activate_import` updates all date pointers in one transaction, marks old snapshots `SUPERSEDED`, new snapshots `ACTIVE`, sets table `LOCKED`, and marks the job `SUCCEEDED`.
- `discard_staged_import` deletes records/values and snapshots with `STAGED` status only.

- [ ] **Step 1: Write failing repository and snapshot tests**

Test state transitions, one unfinished job per table, claim contention with two sessions, expired lease recovery, snapshot version increments, multi-date pointer switching, failure cleanup, and preservation of `ACTIVE`/`SUPERSEDED` rows.

```python
async def test_activation_switches_all_dates_and_supersedes_old_snapshots(session):
    job = DataImportJob(
        table_id=table.id,
        source=DataImportSource.JSON,
        status=DataImportStatus.VALIDATING,
        storage_path="imports/test.json",
        content_sha256="a" * 64,
        size_bytes=2,
        requested_by=context.user_id,
        attempt_count=1,
        lease_owner="test",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(job)
    await session.flush()
    snapshots = await data_import_service.start_staged_import(
        session,
        job,
        {date(2026, 10, 17), date(2026, 10, 18)},
    )
    await data_import_service.activate_import(
        session,
        import_id=job.id,
        worker_id="test",
    )
    active = await data_import_repository.list_current_snapshots(session, table.id)
    assert {item.business_date for item in active} == {
        date(2026, 10, 17),
        date(2026, 10, 18),
    }
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_import_repository.py tests/test_data_snapshot_service.py -v`

Expected: FAIL because import persistence services do not exist.

- [ ] **Step 3: Implement durable state and pointer activation**

Use `SELECT ... FOR UPDATE SKIP LOCKED` for claim. Eligible jobs are `QUEUED`, or `VALIDATING`/`APPLYING` jobs whose lease expired. The claim transaction must set status `VALIDATING`, increment `attempt_count`, store worker/lease, and commit before parsing. On retry, first discard staged snapshots left by the prior attempt. Snapshot versions are allocated under a table lock. `append_staged_rows` inserts records in batches and obtains generated IDs before inserting values. `activate_import` locks the table and current pointers, verifies all expected staged snapshots, updates pointers, marks statuses, sets `locked_at`/`LOCKED`, and commits.

- [ ] **Step 4: Run persistence tests**

Run: `.venv/bin/pytest tests/test_data_import_repository.py tests/test_data_snapshot_service.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/data_import.py app/services/data_import.py tests/test_data_import_repository.py tests/test_data_snapshot_service.py
git commit -m "feat: persist imports with atomic date snapshots"
```

---

### Task 9: Implement Import Orchestration and Worker

**Files:**
- Create: `app/workers/__init__.py`
- Create: `app/workers/data_import.py`
- Modify: `app/services/data_import.py`
- Test: `tests/test_data_import_service.py`
- Test: `tests/test_data_import_worker.py`

**Interfaces:**
- Produces `enqueue_import(..., source: DataImportSource, stored: StoredImportSource, context: AuthContext) -> DataImportJob`.
- Produces `process_import_job(job_id: int, *, worker_id: str) -> None`.
- Produces `run_worker(*, poll_interval: float = 1.0, stop_event: asyncio.Event | None = None) -> None`.
- Worker entrypoint: `.venv/bin/python -m app.workers.data_import`.
- Worker rehydrates the requester with `user_repository.get_by_id` + `build_auth_context`; it fails the job if the user is inactive, `data:import` is absent, or either imported customer is outside current scope.
- Enqueue writes an audit action `data_import:request` with resource type `custom_data_import`. Worker completion/failure uses `record_audit_entry` with the requester actor snapshot and action `data_import:complete`/`data_import:fail`; these audit writes are best-effort and do not replace job state.

- [ ] **Step 1: Write failing orchestration tests**

Cover enqueue, successful JSON/Excel processing, per-row errors, all-or-nothing multi-date behavior, lock acquisition, requester permission/scope drift, lease extension, a second worker not claiming the same job, and restart cleanup of `STAGED` snapshots.

```python
async def test_limited_scope_import_rejects_invisible_counterparty(
    session, sales_context
):
    job = await enqueue_import(..., context=sales_context)
    await process_import_job(job.id, worker_id="worker-1")
    refreshed = await data_import_repository.get_import_job(session, job.id)
    assert refreshed.status == DataImportStatus.FAILED
    assert refreshed.error_count == 1
    assert (
        await data_table_repository.get_table(session, job.table_id).status
        == DataTableStatus.DRAFT
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_import_service.py tests/test_data_import_worker.py -v`

Expected: FAIL because orchestration functions and worker module do not exist.

- [ ] **Step 3: Implement parse/stage/activate lifecycle**

Enqueue locks the data table row, rejects when `has_unfinished_import` is true, stores a job and source, and commits. The worker claims, refreshes its lease, resolves the definition and customer index, parses all rows, stages valid rows, records up to 10,000 errors plus total count, and discards the whole import if any error exists. Update `total_rows`, `valid_rows`, `invalid_rows`, `date_from`, `date_to`, `error_count`, and `progress_percent` after parse and staging phases so the status API reports durable progress. On success call `activate_import`. On failure mark `FAILED`, preserve the source for inspection, and clean staged rows. The worker loop must catch per-job exceptions so one bad import cannot terminate the process. Record request/complete/fail audit actions without adding source values or customer names.

- [ ] **Step 4: Run orchestration and worker tests**

Run: `.venv/bin/pytest tests/test_data_import_service.py tests/test_data_import_worker.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/data_import.py app/workers/__init__.py app/workers/data_import.py tests/test_data_import_service.py tests/test_data_import_worker.py
git commit -m "feat: process data imports in durable workers"
```

---

### Task 10: Add Import and Template APIs

**Files:**
- Create: `app/api/data_imports.py`
- Modify: `app/api/data_tables.py`
- Modify: `app/api/router.py`
- Modify: `app/services/data_table.py`
- Test: `tests/test_data_import_api.py`
- Test: `tests/test_data_import_source_api.py`

**Interfaces:**
- Produces `POST /api/v1/data-tables/{table_id}/imports/excel` accepting `UploadFile` under field `file`, returning 202.
- Produces `POST /api/v1/data-tables/{table_id}/imports/json` accepting `DataImportJsonRequest`, returning 202.
- Produces `GET /api/v1/data-tables/{table_id}/imports`, `GET /.../imports/{import_id}`, `GET /.../imports/{import_id}/errors`, and `GET /.../imports/{import_id}/source`.
- Produces `GET /api/v1/data-tables/{table_id}/template.xlsx` via `build_excel_template`.
- All import endpoints require `data:import`; template requires `data:read`.
- Registers audit metadata for import request routes and denied access; async completion/failure is recorded by the worker from Task 9.
- DRAFT table deletion removes DB history by the cascade from Task 2 and then deletes each private source file best-effort.

- [ ] **Step 1: Write failing import API tests**

Test multipart upload, JSON enqueue, header-less/malformed file rejection, the 100,000-row and 50 MiB guards for both sources, 202 response, status/errors pagination, authenticated source download, template headers, and permission failures. Assert no raw values appear in audit/log messages.

```python
async def test_excel_import_returns_202_and_persists_source(
    client, ops_headers, xlsx_file
):
    response = await client.post(
        f"/api/v1/data-tables/{table_id}/imports/excel",
        headers=ops_headers,
        files={
            "file": (
                "day.xlsx",
                xlsx_file,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "QUEUED"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_import_api.py tests/test_data_import_source_api.py -v`

Expected: FAIL because import routes do not exist.

- [ ] **Step 3: Implement streaming upload, enqueue routes, and cleanup**

Use bounded chunk reads from `UploadFile` into `save_import_bytes`; on any DB enqueue failure delete the saved file. Validate `.xlsx` suffix at the route and actual container in the parser. For JSON, enforce max row count after schema validation and persist canonical JSON. Return task summaries, not job internals containing storage paths. Use `FileResponse` for source download with `Content-Disposition` and never expose the private filesystem path.

- [ ] **Step 4: Run import API tests**

Run: `.venv/bin/pytest tests/test_data_import_api.py tests/test_data_import_source_api.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/data_imports.py app/api/data_tables.py app/api/router.py app/services/data_table.py tests/test_data_import_api.py tests/test_data_import_source_api.py
git commit -m "feat: expose Excel and JSON data import APIs"
```

---

### Task 11: Implement Current-Data Query and Authorization

**Files:**
- Create: `app/schemas/data_query.py`
- Create: `app/repositories/data_query.py`
- Create: `app/services/data_query.py`
- Test: `tests/test_data_query_service.py`
- Test: `tests/test_data_query_authorization.py`

**Interfaces:**
- Produces schemas `DataFieldCondition(field_id, operator, value)`, `DataRecordSearch(date_from=None, date_to=None, upstream_customer_ids=[], downstream_customer_ids=[], conditions=[], sort_field_id=None, sort_direction="desc", page=1, page_size=20)`, `DataRecordLine`, `DataSearchSummary`, `DataSearchResult`, `TableVisibilitySummary`.
- `DataRecordLine` exposes record ID, business date, upstream/downstream customer IDs, current names, deleted flags, source-name snapshots, revenue/cost/gross profit/gross margin, and typed custom values keyed by field ID.
- Produces `visible_customer_id_subquery(context: AuthContext)` using existing `build_customer_scope_filter`.
- Produces `async search_records(session, table_id: int, query: DataRecordSearch, *, context: AuthContext) -> DataSearchResult`.
- Produces `async list_table_visibility_summaries(session, table_ids: Sequence[int], *, context: AuthContext) -> dict[int, TableVisibilitySummary]`; each summary contains visible row count and visible date bounds.
- Operators are AND-only: text `eq|contains`, integer/decimal/date `eq|gte|lte`, boolean `eq`, enum `eq|in`.
- Row predicate is `upstream_customer_id IN visible OR downstream_customer_id IN visible`; ALL scope omits the predicate.
- Summary is computed over the full filtered set, not the page.

- [ ] **Step 1: Write failing query/authorization tests**

Test defaults, date/customer filters, every operator/type, custom-field condition joins, sorting, pagination, totals, gross profit/margin, both visible IDs covering all rows, and `DataScope.ALL` seeing all rows. Test that a row with one visible and one hidden customer is returned in full. Soft-delete one bound customer and assert its historical row remains queryable through the original owner scope, reports `deleted=True`, and is not rebound when another active customer reuses the name.

```python
async def test_either_visible_customer_returns_full_row(session, sales_context):
    result = await data_query_service.search_records(
        session,
        table.id,
        DataRecordSearch(page=1, page_size=20),
        context=sales_context,
    )
    assert [line.upstream_customer_id for line in result.items] == [visible_customer.id]
    assert result.items[0].downstream_customer_id == hidden_counterparty.id
    assert result.items[0].cost == Decimal("1155.14")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_query_service.py tests/test_data_query_authorization.py -v`

Expected: FAIL because query schemas and services do not exist.

- [ ] **Step 3: Implement current-snapshot query builder and typed filters**

Join `DataCurrentSnapshot` and `DataSnapshot` to select current records. Add records filters before count/summary. Add one aliased `DataRecordValue` join per custom condition/sort field; validate field ownership and operator/type compatibility before constructing SQL. Use bound parameters and escape text LIKE wildcards. Load custom values for the page in one query.

Join upstream/downstream `Customer` aliases for current names and status. Return source-name snapshots separately. Use `Decimal` throughout. Add stable `id DESC` tie-break unless a unique sort field is requested.

- [ ] **Step 4: Run query tests**

Run: `.venv/bin/pytest tests/test_data_query_service.py tests/test_data_query_authorization.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/data_query.py app/repositories/data_query.py app/services/data_query.py tests/test_data_query_service.py tests/test_data_query_authorization.py
git commit -m "feat: query current snapshots with customer row scope"
```

---

### Task 12: Add Query API and End-to-End Coverage

**Files:**
- Modify: `app/api/data_tables.py`
- Modify: `tests/test_data_table_api.py`
- Create: `tests/test_data_management_e2e.py`
- Create: `tests/test_data_permissions_e2e.py`

**Interfaces:**
- Produces `POST /api/v1/data-tables/{table_id}/records/search` requiring `data:read`.
- Modifies `GET /api/v1/data-tables` to include each table's visible row count and date bounds from `list_table_visibility_summaries`; no table is omitted when its visible count is zero.
- The search response returns `items`, `total`, `page`, `page_size`, and `summary` inside the standard success envelope.
- Data-record Decimal values serialize as strings; dates serialize as ISO `YYYY-MM-DD`.

- [ ] **Step 1: Write failing end-to-end tests**

Create a table, import two dates, read current rows, replace one date, verify the other date is unchanged, verify superseded rows are not returned, verify row access as sales, and verify no current rows or summaries leak to an unrelated sales user. Also assert the table list includes all definitions while each visible count/date summary is scoped to the caller.

```python
async def test_search_returns_only_current_scoped_rows(
    client, ops_headers, sales_headers
):
    await import_two_dates(client, ops_headers, table_id)
    await replace_day(client, ops_headers, table_id, date(2026, 10, 18))
    response = await client.post(
        f"/api/v1/data-tables/{table_id}/records/search",
        headers=sales_headers,
        json={"page": 1, "page_size": 20},
    )
    body = response.json()["data"]
    assert response.status_code == 200
    assert {item["business_date"] for item in body["items"]} == {
        "2026-10-17",
        "2026-10-18",
    }
    assert body["summary"]["gross_profit"] == "expected"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_data_table_api.py tests/test_data_management_e2e.py tests/test_data_permissions_e2e.py -v`

Expected: FAIL because the search route does not exist.

- [ ] **Step 3: Implement query route and response serializers**

Validate Pydantic request types, call the query service, serialize Decimal as strings, preserve UTC timestamps, and return `success(...)`. Do not expose historical snapshots through any new route. Register read route metadata where existing convention requires it; do not audit successful reads as writes.

- [ ] **Step 4: Run API and end-to-end tests**

Run: `.venv/bin/pytest tests/test_data_table_api.py tests/test_data_management_e2e.py tests/test_data_permissions_e2e.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/data_tables.py tests/test_data_table_api.py tests/test_data_management_e2e.py tests/test_data_permissions_e2e.py
git commit -m "feat: expose scoped current data queries"
```

---

### Task 13: Performance, Security, and Operational Verification

**Files:**
- Create: `scripts/benchmark_data_management.py`
- Test: `tests/test_data_management_hardening.py`
- Modify: any files required by failing hardening tests.

**Interfaces:**
- Benchmark script generates 100,000 rows and 1,000,000 current records with at least 20 custom fields, reports import duration and P95 query/summary latency, and exits nonzero when the spec thresholds are exceeded.
- Hardening tests verify no source path/raw values in logs, leases recover after a process-like session close, invalid xlsx structures are rejected, duplicate migration preflight leaks no names, and all app boot/migration/type/style checks pass.

- [ ] **Step 1: Write failing hardening and benchmark assertions**

Add focused tests for lease expiry, stale staged cleanup, private source traversal, formula workbooks, text boundary, duplicate migration output, and log redaction. Create the benchmark script with explicit `--rows`, `--fields`, and `--table-rows` options.

```python
async def test_failed_import_leaves_no_visible_snapshot(session, import_job):
    await process_import_job(import_job.id, worker_id="worker-test")
    assert (
        await data_import_repository.list_current_snapshots(
            session, import_job.table_id
        )
        == []
    )
    table = await data_table_repository.get_table(session, import_job.table_id)
    assert table.status == DataTableStatus.DRAFT
```

- [ ] **Step 2: Run hardening tests and benchmark in a controlled test database**

Run: `.venv/bin/pytest tests/test_data_management_hardening.py -v`

Expected: PASS after required fixes.

Run: `.venv/bin/python scripts/benchmark_data_management.py --rows 100000 --fields 20 --table-rows 1000000`

Expected: import completes within 15 minutes; list query P95 ≤ 3 seconds and summary P95 ≤ 5 seconds for the documented filtered workload. If the benchmark fails, use `EXPLAIN ANALYZE` and add/adjust indexes or query structure; do not relax the spec threshold without user approval.

- [ ] **Step 3: Run the complete verification suite**

Run: `.venv/bin/ruff check .`

Expected: PASS.

Run: `.venv/bin/ruff format --check .`

Expected: PASS.

Run: `.venv/bin/mypy app`

Expected: PASS.

Run: `.venv/bin/pytest -q`

Expected: all tests pass.

- [ ] **Step 4: Verify migration round trip**

Run:

```bash
JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head
JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic downgrade -1
JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head
```

Expected: all commands succeed; customer-name uniqueness and data tables survive the round trip correctly.

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark_data_management.py tests/test_data_management_hardening.py
git commit -m "test: harden and benchmark data management"
```

If hardening required production-code changes, add only those exact files to the same commit and state them under this task before committing.

---

## Execution Notes

- Task 1 and Task 2 migration revisions must remain linear; do not create a second Alembic head.
- Task 8 and Task 9 are the highest-risk interface boundary. Preserve the exact `DataImportJob` state names and snapshot statuses from Task 2.
- Task 11 must apply the row-scope predicate before count, rows, and summary. Any later export feature must reuse that predicate.
- Task 12 deliberately has no historical snapshot endpoint. Superseded snapshots remain in persistence for future reconciliation work.
- The API process and worker process must share `DATABASE_URL` and `DATA_IMPORT_ROOT`.
- Do not use ad hoc string interpolation for dynamic field values in SQL; all user values stay bound parameters.
