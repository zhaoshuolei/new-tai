# 对接人跟进记录与沟通历史 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为对接人增加跟进记录（= 沟通历史）、可维护的跟进方式、带鉴权下载的附件，以及跨对接人的「今日/逾期待跟进」清单接口。

**Architecture:** 新增三张表 `contact_follow_up_method`、`contact_follow_up`、`contact_follow_up_attachment`。业务逻辑放在 `app/services/follow_up.py`（方式、记录、待跟进查询）与 `app/services/follow_up_attachment.py` + `follow_up_attachment_storage.py`（附件，私有目录 + 按字节识别）。接口在 `app/api/follow_ups.py`，挂在 `/api/v1` 下、沿用 `customer:read/update/delete` 权限码。附件不走静态托管，走鉴权下载接口。

**Tech Stack:** FastAPI、SQLAlchemy 2.x async、asyncmy（MySQL 8）、Alembic、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、标准库 `zipfile` 识别 OOXML。

**Spec:** `docs/superpowers/specs/2026-10-18-contact-follow-ups-design.md`（已批准）。执行者必须同时阅读 spec。

## Global Constraints

- 迁移 `down_revision` 固定为 `26a11cef913c`（当前唯一 head）。
- 不新增权限码、不新增菜单种子。`tests/test_seed_data.py` 的 `EXPECTED_PERMISSION_CODES` 保持不变。
- 错误码：`FOLLOW_UP_NOT_FOUND = 7013`、`FOLLOW_UP_METHOD_NOT_FOUND = 7014`、`FOLLOW_UP_METHOD_NAME_EXISTS = 7015`、`FOLLOW_UP_METHOD_IN_USE = 7016`、`FOLLOW_UP_NOT_OWNER = 7017`、`FOLLOW_UP_ATTACHMENT_NOT_FOUND = 7018`、`FOLLOW_UP_ATTACHMENT_LIMIT = 7019`、`FOLLOW_UP_ATTACHMENT_INVALID = 7020`、`FOLLOW_UP_USER_UNAVAILABLE = 7021`。字段校验沿用 `CUSTOMER_INVALID_FIELD = 7003`。
- 软删除字段统一：`status`（`ACTIVE` / `DELETED`）、`deleted_at`、`deleted_by`。列表与唯一性只针对 `ACTIVE`；删除方式的引用计数**包含已删除记录**。
- 时间入库存 UTC 朴素时间（`DATETIME(6)`）；待跟进按 `Asia/Shanghai` 自然日边界判定。
- 响应时间格式 `+00:00` 的 UTC ISO 8601，复用 `app/schemas/customer.py` 的 `_serialize_utc` 写法。
- 附件：单文件 ≤ 10 MiB（10485760 字节），每条记录 ≤ 5 个；私有目录、鉴权下载；原始文件名走 `X-File-Name` 请求头，不进查询字符串。
- 分页沿用 `app/core/response.py` 的 `Page` 与 `MAX_PAGE_SIZE = 100`。
- 审计只记录对象与动作，不记录沟通内容、附件文件名、附件内容。
- 代码标识符与表名用英文，注释与文档用中文。ruff line-length 88，mypy strict。
- 测试库：`mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test`。运行 alembic 命令时需显式设置 `JWT_SECRET` 与 `CONFIG_ENCRYPTION_KEY`。

## Review Focus

下面 5 类问题 spec 没有逐条给出样例，但最容易在真实使用中出错。每一条都在对应任务的测试里被钉住：

1. **待跟进的时区边界**：`next_follow_up_at` 落在「昨日 23:59」「今日 00:00」「今日 23:59」「明日 00:00」四个点上的归属必须分别是逾期、今日、今日、不进清单（Task 7 / Task 10）。
2. **跨客户、跨对接人的 ID 混用**：用 A 客户的 `customer_id` 搭配 B 客户的对接人 ID 或记录 ID，必须返回 `7005` / `7013`，不得因 ID 存在而泄露存在性（Task 6 / Task 10）。
3. **大页码**：待跟进清单与记录列表 `page` 极大时返回空列表而不是 500（Task 7 / Task 10）。
4. **附件路径篡改**：数据库里的 `storage_path` 被写成 `../media/...`、绝对路径或指向 `MEDIA_ROOT` 时，下载接口必须 404，绝不读出该文件（Task 9）。
5. **跟进人被软删除后记录只读**：`GET` 仍 200 可读，但任何人对它 `PATCH` / `DELETE` 都返回 `7017`（Task 6 / Task 10）。

---

## File Structure

- Create: `app/models/follow_up.py` — `ContactFollowUpMethod`、`ContactFollowUp`、`ContactFollowUpAttachment`。
- Modify: `app/models/__init__.py` — 注册三个模型。
- Create: `migrations/versions/<rev>_contact_follow_ups.py`。
- Modify: `app/core/errors.py` — 7013–7021。
- Modify: `app/core/datetime_utils.py` — `business_day_bounds_utc`。
- Modify: `app/core/config.py` — 私有附件目录与上限配置。
- Create: `app/repositories/follow_up.py` — 方式、记录、待跟进查询、附件的数据访问。
- Create: `app/services/follow_up.py` — 方式与记录业务逻辑、待跟进查询业务逻辑。
- Create: `app/services/follow_up_attachment_storage.py` — 类型识别、私有目录读写（无 DB）。
- Create: `app/services/follow_up_attachment.py` — 附件的权限与持久化。
- Create: `app/schemas/follow_up.py`、`app/api/follow_ups.py`。
- Modify: `app/api/router.py`、`app/main.py`（OpenAPI tag）。
- Modify: `scripts/init_data.py` — 幂等写入初始跟进方式。
- Create: `tests/follow_up_helpers.py`（共享构造函数，签名固定，其他测试只能从这里导入）。
- Create: `tests/test_follow_up_*.py`。
- Modify: `tests/test_datetime_utils.py`、`tests/test_config.py`、`tests/test_app_boot.py`。
- Modify: `.env.example`、`.gitignore`。

---

### Task 1: 错误码与业务日界

**Files:**
- Modify: `app/core/errors.py`
- Modify: `app/core/datetime_utils.py`
- Test: `tests/test_follow_up_error_codes.py`（新建）、`tests/test_datetime_utils.py`

**Interfaces:**
- Produces: `ErrorCode` 新增 9 个成员，名称与数值见 Global Constraints；`app.core.datetime_utils.business_day_bounds_utc(reference: datetime | None = None) -> tuple[datetime, datetime]`，返回**朴素 UTC** 的 `[今日 00:00, 明日 00:00)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_error_codes.py
from app.core.errors import ErrorCode


def test_follow_up_error_codes_match_spec() -> None:
    assert ErrorCode.FOLLOW_UP_NOT_FOUND == 7013
    assert ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND == 7014
    assert ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS == 7015
    assert ErrorCode.FOLLOW_UP_METHOD_IN_USE == 7016
    assert ErrorCode.FOLLOW_UP_NOT_OWNER == 7017
    assert ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND == 7018
    assert ErrorCode.FOLLOW_UP_ATTACHMENT_LIMIT == 7019
    assert ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID == 7020
    assert ErrorCode.FOLLOW_UP_USER_UNAVAILABLE == 7021
```

```python
# tests/test_datetime_utils.py（追加）
def test_business_day_bounds_use_shanghai_midnight() -> None:
    # 2026-10-17 20:00 UTC = 2026-10-18 04:00 上海，因此今日边界是
    # 上海 2026-10-18 00:00 = UTC 2026-10-17 16:00。
    reference = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
    start, end = business_day_bounds_utc(reference)
    assert start == datetime(2026, 10, 17, 16, 0)
    assert end == datetime(2026, 10, 18, 16, 0)
    assert start.tzinfo is None and end.tzinfo is None


def test_business_day_bounds_accept_naive_reference_as_utc() -> None:
    reference = datetime(2026, 10, 17, 20, 0)
    start, end = business_day_bounds_utc(reference)
    assert (start, end) == (
        datetime(2026, 10, 17, 16, 0),
        datetime(2026, 10, 18, 16, 0),
    )
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_error_codes.py tests/test_datetime_utils.py -v`
Expected: FAIL（`ErrorCode` 无成员；`business_day_bounds_utc` 未定义；测试文件缺 `from app.core.datetime_utils import business_day_bounds_utc` 的导入需一并补上）

- [ ] **Step 3: 实现**

在 `app/core/errors.py` 的 `RECORD_NOT_DELETED = 7012` 之后追加 7013–7021。

在 `app/core/datetime_utils.py` 追加（`datetime`、`time`、`timedelta` 已在该模块作用域内可用；朴素参考时间按 UTC 处理，与既有 `to_utc_naive` 口径一致）：

```python
def business_day_bounds_utc(
    reference: datetime | None = None,
) -> tuple[datetime, datetime]:
    """返回业务时区「今日/明日 00:00」的朴素 UTC 边界。"""
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_error_codes.py tests/test_datetime_utils.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/errors.py app/core/datetime_utils.py tests/test_follow_up_error_codes.py tests/test_datetime_utils.py
git commit -m "feat: add follow-up error codes and business day bounds"
```

---

### Task 2: 数据模型与迁移

**Files:**
- Create: `app/models/follow_up.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/<rev>_contact_follow_ups.py`（`<rev>` 由 alembic 生成）
- Test: `tests/test_follow_up_models.py`

**Interfaces:**
- Produces:
  - `ContactFollowUpMethod`（表 `contact_follow_up_method`）：`name: String(64)`、`status: ContactStatus`（默认 `ACTIVE`）、`deleted_at`、`deleted_by`、`active_name`（生成列 `CASE WHEN status = 'ACTIVE' THEN name END`，persisted）、`created_by`、`updated_by`，唯一约束 `uq_contact_follow_up_method_active_name` on `(active_name)`。
  - `ContactFollowUp`（表 `contact_follow_up`）：`contact_id: BigInteger`（FK `customer_contact.id` RESTRICT，索引）、`method_id: BigInteger`（FK `contact_follow_up_method.id` RESTRICT，索引）、`follow_up_user_id: BigInteger`（FK `user.id` RESTRICT，索引）、`follow_up_at: DATETIME(6)`（非空）、`content: Text`（非空）、`next_follow_up_at: DATETIME(6) | None`、`status`、`deleted_at`、`deleted_by`、`created_by: BigInteger`（非空）、`updated_by: BigInteger | None`。索引 `ix_contact_follow_up_contact_status_time` on `(contact_id, status, follow_up_at)`、`ix_contact_follow_up_status_next` on `(status, next_follow_up_at)`。
  - `ContactFollowUpAttachment`（表 `contact_follow_up_attachment`）：`follow_up_id: BigInteger`（FK `contact_follow_up.id` RESTRICT，索引）、`storage_path: String(512)`（唯一）、`original_name: String(255) | None`、`content_type: String(127)`（非空）、`size_bytes: BigInteger`（非空）、`created_by: BigInteger`（非空）。
- 三个模型都继承 `app.db.base.Base`（提供 `id`、`created_at`、`updated_at`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_models.py
from sqlalchemy import Computed, inspect

from app.models.follow_up import (
    ContactFollowUp,
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)


def test_method_columns_and_generated_name() -> None:
    columns = {c.name for c in inspect(ContactFollowUpMethod).columns}
    assert {"name", "status", "active_name", "deleted_at", "deleted_by"} <= columns
    computed = ContactFollowUpMethod.__table__.c.active_name.computed
    assert isinstance(computed, Computed)
    assert "status = 'ACTIVE'" in str(computed.sqltext)
    unique_columns = {
        tuple(c.name for c in index.columns)
        for index in ContactFollowUpMethod.__table__.indexes
        if index.unique
    }
    assert ("active_name",) in unique_columns


def test_follow_up_columns() -> None:
    columns = {c.name for c in inspect(ContactFollowUp).columns}
    assert {
        "contact_id",
        "method_id",
        "follow_up_user_id",
        "follow_up_at",
        "content",
        "next_follow_up_at",
        "status",
        "created_by",
    } <= columns
    assert ContactFollowUp.__table__.c.content.nullable is False
    assert ContactFollowUp.__table__.c.follow_up_at.nullable is False
    assert ContactFollowUp.__table__.c.next_follow_up_at.nullable is True


def test_follow_up_indexes_exist() -> None:
    index_names = {index.name for index in ContactFollowUp.__table__.indexes}
    assert "ix_contact_follow_up_contact_status_time" in index_names
    assert "ix_contact_follow_up_status_next" in index_names


def test_attachment_columns() -> None:
    columns = {c.name for c in inspect(ContactFollowUpAttachment).columns}
    assert {
        "follow_up_id",
        "storage_path",
        "original_name",
        "content_type",
        "size_bytes",
        "created_by",
    } <= columns
    # DOCX 的 MIME 类型长 71 字符，64 存不下。
    assert ContactFollowUpAttachment.__table__.c.content_type.type.length >= 71
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_models.py -v`
Expected: FAIL（`app.models.follow_up` 不存在）

- [ ] **Step 3: 实现模型与迁移**

按 `app/models/contact.py` 的写法创建 `app/models/follow_up.py`（`ContactStatus` 从 `app.models.contact` 导入）。在 `app/models/__init__.py` 导入三个模型并加入 `__all__`。

生成迁移：

```bash
JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic revision -m "contact follow ups"
```

填写 `down_revision = "26a11cef913c"`，`revision` 用生成值。`upgrade()` 依次 `create_table` 方式、记录、附件（顺序保证外键目标已存在），`downgrade()` 反序 `drop_table`。枚举复用 `contact_status`（`sa.Enum("ACTIVE", "DELETED", name="contact_status")`，与其他表一致，不重复创建类型）。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_models.py -v`
Expected: PASS

Run: `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head && JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic downgrade -1 && JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head`
Expected: 三条命令均成功，无报错

- [ ] **Step 5: 提交**

```bash
git add app/models/follow_up.py app/models/__init__.py migrations/versions/ tests/test_follow_up_models.py
git commit -m "feat: add follow-up models and migration"
```

---

### Task 3: 私有附件目录配置

**Files:**
- Modify: `app/core/config.py`
- Modify: `.env.example`、`.gitignore`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.follow_up_attachment_root: str`（默认 `attachments`，`validate_default=True`）、`Settings.follow_up_attachment_max_bytes: int = 10485760`、`Settings.follow_up_attachment_max_per_record: int = 5`；`app.core.config.resolve_follow_up_attachment_root(value: str, media_root: str) -> Path`。根目录必须是绝对路径、不得是工作目录或其祖先、且不得与解析后的 `MEDIA_ROOT` 重叠（相等或互为祖先/后代）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py（追加）
import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    values = {
        "database_url": "mysql+asyncmy://u:p@localhost:3306/db",
        "redis_url": "redis://localhost:6379/15",
        "jwt_secret": "secret",
        "config_encryption_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_attachment_root_defaults_below_working_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _settings(media_root=str(tmp_path / "media"))
    assert settings.follow_up_attachment_root.startswith(str(tmp_path))
    assert settings.follow_up_attachment_max_bytes == 10485760
    assert settings.follow_up_attachment_max_per_record == 5


@pytest.mark.parametrize("suffix", ["", "/nested", "/../media/nested"])
def test_attachment_root_must_not_overlap_media_root(tmp_path, monkeypatch, suffix) -> None:
    monkeypatch.chdir(tmp_path)
    media_root = tmp_path / "media"
    with pytest.raises(ValidationError):
        _settings(
            media_root=str(media_root),
            follow_up_attachment_root=str(media_root) + suffix,
        )


def test_media_root_inside_attachment_root_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        _settings(
            media_root=str(tmp_path / "files" / "media"),
            follow_up_attachment_root=str(tmp_path / "files"),
        )
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL（`follow_up_attachment_root` 不存在）

- [ ] **Step 3: 实现**

在 `app/core/config.py` 增加 `resolve_follow_up_attachment_root`（复用 `resolve_media_root` 的基础校验，再做重叠判断），并给 `Settings` 增加三个字段。重叠校验需要同时看到两个已解析路径，因此用 `@model_validator(mode="after")` 而不是字段级校验器。`.env.example` 增加三个注释项，`.gitignore` 增加 `/attachments/`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_config.py tests/test_system_setting_api.py -v`
Expected: PASS（既有 `MEDIA_ROOT` 校验行为不变）

- [ ] **Step 5: 提交**

```bash
git add app/core/config.py .env.example .gitignore tests/test_config.py
git commit -m "feat: add private attachment root configuration"
```

---

### Task 4: 测试 helper 与仓储层（方式、记录）

**Files:**
- Create: `tests/follow_up_helpers.py`
- Create: `app/repositories/follow_up.py`
- Test: `tests/test_follow_up_repository.py`

**Interfaces:**
- Produces（测试 helper，签名固定）：
  - `add_method(session, *, name: str | None = None, status: ContactStatus = ContactStatus.ACTIVE) -> ContactFollowUpMethod`
  - `add_follow_up(session, contact: CustomerContact, method: ContactFollowUpMethod, *, follow_up_user: User, content: str | None = None, follow_up_at: datetime | None = None, next_follow_up_at: datetime | None = None, status: ContactStatus = ContactStatus.ACTIVE) -> ContactFollowUp`
  - `add_attachment(session, follow_up: ContactFollowUp, *, storage_path: str | None = None, original_name: str | None = None, content_type: str = "application/pdf", size_bytes: int = 1) -> ContactFollowUpAttachment`
- Produces（`app/repositories/follow_up.py`）：
  - `get_method(session, method_id) -> ContactFollowUpMethod | None`
  - `lock_method_for_update(session, method_id) -> ContactFollowUpMethod | None`
  - `lock_method_shared(session, method_id) -> ContactFollowUpMethod | None`
  - `list_methods(session, *, status: ContactStatus) -> list[ContactFollowUpMethod]`（按 `name`、`id` 排序）
  - `lock_follow_up_ids_referencing_method(session, method_id) -> list[int]`（含已删除记录，`FOR UPDATE` 当前读）
  - `add_follow_up(session, follow_up) -> ContactFollowUp`（add + flush + refresh）
  - `get_follow_up(session, contact_id, follow_up_id) -> ContactFollowUp | None`
  - `lock_follow_up_for_update(session, contact_id, follow_up_id) -> ContactFollowUp | None`
  - `list_follow_ups(session, contact_id, *, status, follow_up_user_id: int | None, page, page_size) -> tuple[list[FollowUpRow], int]`（`follow_up_at` 倒序、`id` 倒序）
  - `get_follow_up_row(session, follow_up_id) -> FollowUpRow | None`
  - `count_attachments(session, follow_up_id: int) -> int`
  - `list_attachments(session, follow_up_ids: list[int]) -> dict[int, list[ContactFollowUpAttachment]]`（批量取，避免列表接口 N+1；按 `id` 升序）
  - `get_attachment(session, follow_up_id: int, attachment_id: int) -> ContactFollowUpAttachment | None`
  - `add_attachment(session, attachment) -> ContactFollowUpAttachment`（add + flush + refresh）
  - `delete_attachment_row(session, attachment) -> None`（`session.delete` + flush）
  - `FollowUpRow`（frozen dataclass）：`follow_up: ContactFollowUp`、`contact_name: str`、`method_name: str`、`follow_up_user_nickname: str | None`、`follow_up_user_username: str | None`、`created_by_nickname: str | None`、`created_by_username: str | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_repository.py
from datetime import UTC, datetime, timedelta

import pytest

from app.models.contact import ContactStatus
from app.repositories import follow_up as follow_up_repository
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


async def test_list_methods_filters_status_and_sorts(session) -> None:  # noqa: F811
    await add_method(session, name="微信")
    await add_method(session, name="电话")
    await add_method(session, name="已删", status=ContactStatus.DELETED)

    methods = await follow_up_repository.list_methods(
        session, status=ContactStatus.ACTIVE
    )

    assert [method.name for method in methods] == ["微信", "电话"]


async def test_list_follow_ups_orders_newest_first_and_pages(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for index in range(3):
        await add_follow_up(
            session,
            contact,
            method,
            follow_up_user=user,
            content=f"第{index}条",
            follow_up_at=base + timedelta(days=index),
        )

    rows, total = await follow_up_repository.list_follow_ups(
        session,
        contact.id,
        status=ContactStatus.ACTIVE,
        follow_up_user_id=None,
        page=1,
        page_size=2,
    )

    assert total == 3
    assert [row.follow_up.content for row in rows] == ["第2条", "第1条"]
    assert rows[0].contact_name == contact.name
    assert rows[0].method_name == "电话"
    assert rows[0].follow_up_user_username == user.username


async def test_reference_count_includes_deleted_records(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    await add_follow_up(session, contact, method, follow_up_user=user)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=user,
        status=ContactStatus.DELETED,
    )

    ids = await follow_up_repository.lock_follow_up_ids_referencing_method(
        session, method.id
    )

    assert len(ids) == 2


async def test_method_shared_lock_returns_row(session) -> None:  # noqa: F811
    method = await add_method(session, name="拜访")
    locked = await follow_up_repository.lock_method_shared(session, method.id)
    assert locked is not None and locked.id == method.id


async def test_list_attachments_groups_by_record(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    first = await add_follow_up(session, contact, method, follow_up_user=user)
    second = await add_follow_up(session, contact, method, follow_up_user=user)
    first_attachment = await add_attachment(session, first, original_name="a.pdf")
    await add_attachment(session, second, original_name="b.pdf")

    grouped = await follow_up_repository.list_attachments(
        session, [first.id, second.id]
    )

    assert [row.id for row in grouped[first.id]] == [first_attachment.id]
    assert [row.original_name for row in grouped[second.id]] == ["b.pdf"]
    assert grouped.get(999_999, []) == []


async def test_count_attachments(session) -> None:  # noqa: F811
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    record = await add_follow_up(session, contact, method, follow_up_user=user)
    await add_attachment(session, record)
    await add_attachment(session, record)

    assert await follow_up_repository.count_attachments(session, record.id) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_repository.py -v`
Expected: FAIL（`tests.follow_up_helpers` 与 `app.repositories.follow_up` 不存在）

- [ ] **Step 3: 实现**

`tests/follow_up_helpers.py` 按 `tests/contact_helpers.py` 的写法创建；`add_method` 不填 `active_name`（生成列由数据库计算），`add_follow_up` 的 `created_by` 默认取 `follow_up_user.id`，`follow_up_at` 默认 `datetime(2026, 10, 1, 2, 0)`（固定值，避免测试依赖当前时间）。

`app/repositories/follow_up.py` 按 `app/repositories/contact.py` 的写法；`list_follow_ups` 与 `get_follow_up_row` 需要 join `CustomerContact`（取 `name`）、`ContactFollowUpMethod`（取 `name`）、两次 `aliased(User)`（取跟进人与录入人的 `nickname`、`username`）。`lock_follow_up_ids_referencing_method` 用 `select(ContactFollowUp.id).where(...).with_for_update()` 后取 `scalars().all()`，不写 `COUNT`（理由同 `contact_repository.lock_contact_ids_referencing` 的注释：REPEATABLE READ 下普通 COUNT 会漏掉并发新插入的行）。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_repository.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/repositories/follow_up.py tests/follow_up_helpers.py tests/test_follow_up_repository.py
git commit -m "feat: add follow-up repository and test helpers"
```

---

### Task 5: 跟进方式服务

**Files:**
- Create: `app/services/follow_up.py`
- Test: `tests/test_follow_up_method_service.py`

**Interfaces:**
- Consumes: `follow_up_repository`（Task 4）、`ErrorCode`（Task 1）。
- Produces:
  - `list_methods(session, *, context) -> list[ContactFollowUpMethod]`
  - `list_deleted_methods(session, *, context) -> list[ContactFollowUpMethod]`
  - `create_method(session, name: str, *, context) -> ContactFollowUpMethod`
  - `update_method(session, method_id: int, name: str, *, context) -> ContactFollowUpMethod`
  - `delete_method(session, method_id: int, *, context) -> None`
  - `restore_method(session, method_id: int, *, context) -> ContactFollowUpMethod`
- 审计：`@audited(action="follow_up_method:create"|"update"|"delete"|"restore", resource_type="contact_follow_up_method", resource_id_param="method_id")`；create 用 `resource_id_attr="id"`。
- 权限：非 `context.is_super_admin` 抛 `BusinessError(ErrorCode.FORBIDDEN, "Permission denied", http_status=403)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_method_service.py
import pytest

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.services import follow_up as follow_up_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


async def test_non_super_admin_cannot_write_methods(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_method(session, "钉钉", context=ctx)
    assert error.value.code == ErrorCode.FORBIDDEN

    methods = await follow_up_service.list_methods(session, context=ctx)
    assert methods == []


async def test_method_name_must_be_unique_among_active(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    await follow_up_service.create_method(session, "电话", context=ctx)

    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_method(session, "  电话  ", context=ctx)
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS


async def test_deleted_method_frees_its_name(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    created = await follow_up_service.create_method(session, "电话", context=ctx)
    await follow_up_service.delete_method(session, created.id, context=ctx)

    recreated = await follow_up_service.create_method(session, "电话", context=ctx)
    assert recreated.id != created.id


async def test_method_in_use_cannot_be_deleted(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    await add_follow_up(session, contact, method, follow_up_user=user)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=user,
        status=ContactStatus.DELETED,
    )

    with pytest.raises(BusinessError) as error:
        await follow_up_service.delete_method(session, method.id, context=ctx)
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_IN_USE


async def test_unused_method_can_be_deleted_and_restored(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await follow_up_service.create_method(session, "电话", context=ctx)

    await follow_up_service.delete_method(session, method.id, context=ctx)
    assert method.status == ContactStatus.DELETED

    restored = await follow_up_service.restore_method(session, method.id, context=ctx)
    assert restored.status == ContactStatus.ACTIVE


async def test_restore_conflicts_with_active_name(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    deleted = await add_method(session, name="电话", status=ContactStatus.DELETED)
    await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.restore_method(session, deleted.id, context=ctx)
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS


async def test_restore_active_method_returns_7012(session) -> None:  # noqa: F811
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    method = await add_method(session, name="电话")

    with pytest.raises(BusinessError) as error:
        await follow_up_service.restore_method(session, method.id, context=ctx)
    assert error.value.code == ErrorCode.RECORD_NOT_DELETED
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_method_service.py -v`
Expected: FAIL（`app.services.follow_up` 不存在）

- [ ] **Step 3: 实现**

`create_method` / `update_method`：`_require_super_admin` → `_normalize_method_name`（去空白、非空、≤64，失败 `7003`）→ 事务内先查未删除同名（`7015`）→ 写入，包在 `session.begin_nested()` 里捕获 `IntegrityError` 转 `7015`（写法照抄 `app/services/contact.py` 的 `_commit_create`）。

`delete_method`：`_require_super_admin` → `lock_method_for_update`（不存在或已删除 `7014`）→ `lock_follow_up_ids_referencing_method`，非空则 `7016` → 置 `DELETED`、写 `deleted_at`/`deleted_by`、commit。

`restore_method`：`_require_super_admin` → 取行（不存在 `7014`）；未删除 `7012`；同名冲突 `7015`（同样捕获 `IntegrityError`）；清空删除字段、commit。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_method_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/follow_up.py tests/test_follow_up_method_service.py
git commit -m "feat: add follow-up method service"
```

---

### Task 6: 跟进记录服务

**Files:**
- Modify: `app/services/follow_up.py`（追加记录部分）
- Test: `tests/test_follow_up_service.py`

**Interfaces:**
- Consumes: Task 4 的仓储函数；`apply_user_scope`（`app.services.data_scope`）；`build_customer_scope_filter`。
- Produces:
  - `require_visible_active_customer(session, customer_id, *, context) -> None`（不可见/已删除抛 `7001`）
  - `require_active_contact(session, customer_id, contact_id) -> CustomerContact`（不属于该客户或已删除抛 `7005`）
  - `require_active_follow_up(session, contact_id, follow_up_id) -> ContactFollowUp`（不存在或已删除抛 `7013`）
  - `require_follow_up_owner(record: ContactFollowUp, *, context) -> None`（不是当前跟进人抛 `7017`）
  - 记录读写的六个公开函数：`list_follow_ups(session, customer_id, contact_id, *, context, page, page_size) -> tuple[list[FollowUpRow], int]`、`list_deleted_follow_ups(同样参数) -> tuple[list[FollowUpRow], int]`（只返回 `follow_up_user_id == context.user_id` 的记录）、`create_follow_up(session, customer_id, contact_id, payload: FollowUpCreate, *, context) -> FollowUpRow`、`update_follow_up(session, customer_id, contact_id, follow_up_id, payload: FollowUpUpdate, *, context) -> FollowUpRow`、`delete_follow_up(session, customer_id, contact_id, follow_up_id, *, context) -> None`、`restore_follow_up(同上) -> FollowUpRow`。
  - 四个守卫函数是**公开**的（Task 9 的附件服务要从 `app.services.follow_up` 导入），不是下划线私有函数。
- `FollowUpCreate` / `FollowUpUpdate` 定义在 Task 10 的 `app/schemas/follow_up.py`；本任务先创建该 schema 文件中的这两个请求模型（响应模型留到 Task 10 追加），避免服务层依赖后置类型。字段：`follow_up_user_id: int | None`、`follow_up_at: datetime | None`、`method_id: int`（Update 中可空）、`content: str | None`、`next_follow_up_at: datetime | None`，均 `extra="forbid"`。
- 审计：`@audited(action="contact_follow_up:create"|"update"|"delete"|"restore", resource_type="contact_follow_up", resource_id_param="follow_up_id")`；create 用 `resource_id_attr="id"`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_service.py
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.role import DataScope
from app.schemas.follow_up import FollowUpCreate, FollowUpUpdate
from app.services import follow_up as follow_up_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


async def _fixture(session):  # noqa: F811
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    ctx = make_context(owner, permission_codes={"customer:read", "customer:update", "customer:delete"})
    return owner, customer, contact, method, ctx


async def test_create_defaults_follow_up_user_to_caller(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    created = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="电话沟通预算"),
        context=ctx,
    )
    assert created.follow_up.follow_up_user_id == owner.id
    assert created.follow_up.created_by == owner.id
    assert created.method_name == "电话"


async def test_create_rejects_unavailable_follow_up_user(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    invisible = await make_user(session)
    # 用 SELF 范围调用：只能看到自己名下的客户，指定他人作为跟进人 → 不可见 → 7021
    hidden = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
        data_scope=DataScope.SELF,
    )
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_follow_up(
            session,
            customer.id,
            contact.id,
            FollowUpCreate(
                method_id=method.id,
                content="x",
                follow_up_user_id=invisible.id,
            ),
            context=hidden,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_USER_UNAVAILABLE


async def test_create_validates_content_and_times(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    now = datetime.now(UTC).replace(tzinfo=None)
    cases = [
        {"content": "   "},
        {"content": "x" * 5001},
        {"content": "ok", "follow_up_at": now + timedelta(minutes=30)},
        {
            "content": "ok",
            "follow_up_at": now,
            "next_follow_up_at": now - timedelta(minutes=1),
        },
    ]
    for fields in cases:
        with pytest.raises(BusinessError) as error:
            await follow_up_service.create_follow_up(
                session,
                customer.id,
                contact.id,
                FollowUpCreate(method_id=method.id, **fields),
                context=ctx,
            )
        assert error.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_create_rejects_deleted_method(session) -> None:  # noqa: F811
    owner, customer, contact, _, ctx = await _fixture(session)
    deleted = await add_method(session, name="旧方式", status=ContactStatus.DELETED)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_follow_up(
            session,
            customer.id,
            contact.id,
            FollowUpCreate(method_id=deleted.id, content="x"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND


async def test_foreign_customer_contact_pair_returns_7005(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    other_customer = await make_customer_row(session, owner=owner)
    with pytest.raises(BusinessError) as error:
        await follow_up_service.create_follow_up(
            session,
            other_customer.id,
            contact.id,
            FollowUpCreate(method_id=method.id, content="x"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_only_owner_can_update_and_delete(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    other = await make_user(session)
    decoy = await make_user(session)
    created = await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=other,
        content="别人的记录",
    )
    ctx = make_context(decoy, permission_codes={"customer:read", "customer:update", "customer:delete"})
    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            FollowUpUpdate(content="改一下"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER

    with pytest.raises(BusinessError) as error:
        await follow_up_service.delete_follow_up(
            session, customer.id, contact.id, created.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER


async def test_super_admin_cannot_edit_others_record(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    author = await make_user(session)
    created = await add_follow_up(
        session, contact, method, follow_up_user=author, content="原始"
    )
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
        is_super_admin=True,
    )
    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            FollowUpUpdate(content="超管改"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER


async def test_transfer_moves_edit_rights(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    colleague = await make_user(session)
    created = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="原始"),
        context=ctx,
    )
    await follow_up_service.update_follow_up(
        session,
        customer.id,
        contact.id,
        created.follow_up.id,
        FollowUpUpdate(follow_up_user_id=colleague.id),
        context=ctx,
    )

    # 原跟进人（= owner）已不再拥有编辑权
    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.follow_up.id,
            FollowUpUpdate(content="再改"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER

    colleague_ctx = make_context(
        colleague,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    updated = await follow_up_service.update_follow_up(
        session,
        customer.id,
        contact.id,
        created.follow_up.id,
        FollowUpUpdate(content="新跟进人改的"),
        context=colleague_ctx,
    )
    assert updated.follow_up.content == "新跟进人改的"


async def test_recycle_bin_lists_only_own_records(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    mine = await follow_up_service.create_follow_up(
        session,
        customer.id,
        contact.id,
        FollowUpCreate(method_id=method.id, content="我的"),
        context=ctx,
    )
    await follow_up_service.delete_follow_up(
        session, customer.id, contact.id, mine.follow_up.id, context=ctx
    )
    other = await make_user(session)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=other,
        status=ContactStatus.DELETED,
    )

    rows, total = await follow_up_service.list_deleted_follow_ups(
        session, customer.id, contact.id, context=ctx, page=1, page_size=20
    )
    assert total == 1
    assert rows[0].follow_up.id == mine.follow_up.id


async def test_deleted_contact_hides_records(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    await add_follow_up(session, contact, method, follow_up_user=owner)
    contact.status = ContactStatus.DELETED
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await follow_up_service.list_follow_ups(
            session, customer.id, contact.id, context=ctx, page=1, page_size=20
        )
    assert error.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_record_of_soft_deleted_user_stays_readable_but_read_only(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    author = await make_user(session)
    created = await add_follow_up(
        session, contact, method, follow_up_user=author, content="离职前写的"
    )
    author.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    await session.flush()
    ctx = make_context(owner, permission_codes={"customer:read", "customer:update", "customer:delete"})

    rows, total = await follow_up_service.list_follow_ups(
        session, customer.id, contact.id, context=ctx, page=1, page_size=20
    )
    assert total == 1

    with pytest.raises(BusinessError) as error:
        await follow_up_service.update_follow_up(
            session,
            customer.id,
            contact.id,
            created.id,
            FollowUpUpdate(content="想改"),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_service.py -v`
Expected: FAIL（服务函数与 `app.schemas.follow_up` 不存在）

- [ ] **Step 3: 实现**

创建 `app/schemas/follow_up.py`，本任务只放 `FollowUpCreate`、`FollowUpUpdate`（`extra="forbid"`，`content: str | None = None`，长度不在 pydantic 层限制，服务层返回 `7003`，与 `schemas/contact.py` 的做法一致）。

`app/services/follow_up.py` 追加：
- `require_visible_active_customer`、`require_active_contact`：逻辑同 `app/services/contact.py` 的两个私有函数（可见性用 `apply_customer_scope` + `Customer.status == ACTIVE`），但本模块导出为公开函数供 Task 9 复用。
- `require_active_follow_up(session, contact_id, follow_up_id)` → 不存在或已删除 `7013`。
- `require_follow_up_owner(record, *, context)` → `record.follow_up_user_id != context.user_id` 抛 `7017`。
- `_resolve_follow_up_user(session, user_id, context)`：`apply_user_scope(select(User.id).where(User.id == user_id, User.deleted_at.is_(None), User.status == UserStatus.ACTIVE), context)`，为空抛 `7021`。
- `_validate_fields(changes)`：`content` 去空白非空且 ≤5000；`follow_up_at` 与 `now_naive() + 5min` 比较；`next_follow_up_at` 必须严格晚于生效的 `follow_up_at`（更新时用记录上的值）；违反抛 `7003`。
- **时间归一化**：入参 `follow_up_at` / `next_follow_up_at` 可能带时区（API 客户端传 `+00:00`），必须先经 `app.core.datetime_utils.to_utc_naive` 转成朴素 UTC 再比较与入库；否则 aware 与 naive 比较会抛 `TypeError`。更新时用记录上已有的朴素值参与比较。
- `_now_naive() -> datetime`：`datetime.now(UTC).replace(tzinfo=None)`。
- 写操作先 `lock_method_shared`（同时校验未删除，`7014`），再校验用户、字段，最后写入/commit。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/follow_up.py app/schemas/follow_up.py tests/test_follow_up_service.py
git commit -m "feat: add follow-up record service"
```

---

### Task 7: 待跟进查询

**Files:**
- Modify: `app/repositories/follow_up.py`（追加待跟进查询）
- Modify: `app/services/follow_up.py`（追加待跟进业务函数）
- Test: `tests/test_follow_up_due_service.py`

**Interfaces:**
- Consumes: `business_day_bounds_utc`（Task 1）、`build_customer_scope_filter`（既有）。
- Produces（`app/repositories/follow_up.py`）：
  - `DueFollowUpRow`（frozen dataclass）：`follow_up: ContactFollowUp`、`contact: CustomerContact`、`function_name: str`、`customer: Customer`、`follow_up_user_nickname: str | None`、`follow_up_user_username: str | None`、`due_state: Literal["today", "overdue"]`
  - `list_due_follow_ups(session, *, scope_filter, due_state: Literal["today", "overdue", "all"], today_start: datetime, tomorrow_start: datetime, follow_up_user_id: int | None, page: int, page_size: int) -> tuple[list[DueFollowUpRow], int]`
  - `DueCustomerRow`（frozen dataclass）：`customer_id: int`、`customer_name: str`、`due_count: int`、`overdue_count: int`、`earliest_next_follow_up_at: datetime`
  - `list_due_customers(session, *, scope_filter, due_state, today_start, tomorrow_start, follow_up_user_id, page, page_size) -> tuple[list[DueCustomerRow], int]`
  - `count_due_states(session, *, scope_filter, today_start, tomorrow_start, follow_up_user_id) -> tuple[int, int]`（返回 `(today_count, overdue_count)`）
- Produces（`app/services/follow_up.py`）：
  - `list_due_follow_ups(session, *, context, due: Literal["today","overdue","all"], follow_up_user_id: int | None, page: int, page_size: int) -> tuple[list[DueFollowUpRow], int]`
  - `list_due_customers(session, *, context, due, follow_up_user_id, page, page_size) -> tuple[list[DueCustomerRow], int]`
  - `due_summary(session, *, context, follow_up_user_id: int | None) -> tuple[int, int]`

**实现要点（测试无法唯一确定的部分）：** 待跟进的候选集是「每个对接人 `follow_up_at` 最大的未删除记录」：

```sql
row_number() OVER (PARTITION BY contact_id ORDER BY follow_up_at DESC, id DESC) AS rn
-- WHERE status = 'ACTIVE'
-- 过滤 rn = 1 AND next_follow_up_at IS NOT NULL
```

`due_state` 由 `next_follow_up_at < today_start` 为 `overdue`，否则 `today`。候选子查询再 join `CustomerContact`（`status='ACTIVE'`）与 `Customer`（`status='ACTIVE'`），套用传入的 `scope_filter`（`build_customer_scope_filter` 只引用 `Customer` 列）。`due=all` 时同时包含 `overdue` 与 `today`（即 `next_follow_up_at < tomorrow_start`）。排序：`next_follow_up_at` 升序、`follow_up_id` 升序。按客户聚合时以 `customer_id` 分组，`due_count` 为该客户待跟进对接人数、`overdue_count` 为其中逾期数、`earliest_next_follow_up_at` 取 `min`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_due_service.py
from datetime import UTC, datetime, timedelta

import pytest

from app.models.contact import ContactStatus
from app.services import follow_up as follow_up_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)

# 固定「现在」：上海 2026-10-18 04:00 → 今日边界 = UTC 2026-10-17 16:00
NOW = datetime(2026, 10, 17, 20, 0)          # 朴素 UTC
TODAY_START = datetime(2026, 10, 17, 16, 0)
YESTERDAY_LAST = datetime(2026, 10, 17, 15, 59, 59)
TODAY_LAST = datetime(2026, 10, 18, 15, 59, 59)
TOMORROW_START = datetime(2026, 10, 18, 16, 0)


async def _setup(session, next_follow_up_at, follow_up_user=None):  # noqa: F811
    owner = follow_up_user or await make_user(session)
    customer = await make_customer_row(session, owner=owner, name="天曦科技")
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function, name="张三")
    method = await add_method(session, name="电话")
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START,
        next_follow_up_at=next_follow_up_at,
    )
    ctx = make_context(owner, permission_codes={"customer:read"})
    return owner, customer, contact, ctx


@pytest.mark.parametrize(
    ("next_follow_up_at", "expected"),
    [
        (YESTERDAY_LAST, ["overdue"]),
        (TODAY_START, ["today"]),
        (TODAY_LAST, ["today"]),
        (TOMORROW_START, []),
        (None, []),
    ],
)
async def test_due_boundaries(session, next_follow_up_at, expected, monkeypatch) -> None:  # noqa: F811
    placeholder = await make_user(session)  # 保证 session 已连接
    _, customer, _, ctx = await _setup(session, next_follow_up_at, follow_up_user=placeholder)
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, total = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=None,
        page=1,
        page_size=20,
    )

    keys = [row.due_state for row in rows if row.customer.id == customer.id]
    assert keys == expected


async def test_only_latest_record_forms_current_plan(session, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, ctx = await _setup(session, YESTERDAY_LAST)
    method = await add_method(session, name="微信")
    # 更新的记录没有下次计划 → 该对接人离开清单
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START + timedelta(hours=1),
        next_follow_up_at=None,
    )
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session, context=ctx, due="all", follow_up_user_id=None, page=1, page_size=20
    )
    assert [row.customer.id for row in rows if row.customer.id == customer.id] == []


async def test_due_filter_and_summary_counts(session, monkeypatch) -> None:  # noqa: F811
    owner = await make_user(session)
    overdue_customer = await make_customer_row(session, owner=owner, name="逾期客户")
    today_customer = await make_customer_row(session, owner=owner, name="今日客户")
    function = await add_function(session, name="商务", customer_id=None)
    method = await add_method(session, name="电话")
    overdue_contact = await add_contact(session, overdue_customer, function)
    today_contact = await add_contact(session, today_customer, function)
    await add_follow_up(
        session,
        overdue_contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START,
        next_follow_up_at=YESTERDAY_LAST,
    )
    await add_follow_up(
        session,
        today_contact,
        method,
        follow_up_user=owner,
        follow_up_at=TODAY_START,
        next_follow_up_at=TODAY_LAST,
    )
    ctx = make_context(owner, permission_codes={"customer:read"})
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    overdue_rows, _ = await follow_up_service.list_due_follow_ups(
        session, context=ctx, due="overdue", follow_up_user_id=None, page=1, page_size=20
    )
    assert [row.customer.name for row in overdue_rows] == ["逾期客户"]

    summary = await follow_up_service.due_summary(
        session, context=ctx, follow_up_user_id=None
    )
    assert summary == (1, 1)

    customer_rows, total = await follow_up_service.list_due_customers(
        session, context=ctx, due="all", follow_up_user_id=None, page=1, page_size=20
    )
    assert total == 2
    assert [(row.customer_name, row.due_count, row.overdue_count) for row in customer_rows] == [
        ("逾期客户", 1, 1),
        ("今日客户", 1, 0),
    ]


async def test_follow_up_user_filter(session, monkeypatch) -> None:  # noqa: F811
    owner, _, _, ctx = await _setup(session, YESTERDAY_LAST)
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=owner.id,
        page=1,
        page_size=20,
    )
    assert len(rows) == 1

    rows, _ = await follow_up_service.list_due_follow_ups(
        session,
        context=ctx,
        due="all",
        follow_up_user_id=owner.id + 10_000,
        page=1,
        page_size=20,
    )
    assert rows == []


async def test_large_page_returns_empty(session, monkeypatch) -> None:  # noqa: F811
    _, _, _, ctx = await _setup(session, YESTERDAY_LAST)
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session, context=ctx, due="all", follow_up_user_id=None, page=10_000, page_size=20
    )
    assert rows == []


async def test_scope_filter_hides_other_department(session, monkeypatch) -> None:  # noqa: F811
    from app.models.role import DataScope

    other_owner = await make_user(session)
    await _setup(session, YESTERDAY_LAST, follow_up_user=other_owner)
    viewer = await make_user(session)
    ctx = make_context(
        viewer,
        permission_codes={"customer:read"},
        data_scope=DataScope.SELF,
    )
    monkeypatch.setattr(follow_up_service, "_now_naive", lambda: NOW)

    rows, _ = await follow_up_service.list_due_follow_ups(
        session, context=ctx, due="all", follow_up_user_id=None, page=1, page_size=20
    )
    assert rows == []
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_due_service.py -v`
Expected: FAIL（待跟进函数不存在）

- [ ] **Step 3: 实现**

仓储层按上面的 SQL 要点实现；`count_due_states` 与 `list_due_customers` 复用同一个候选子查询，避免三份口径漂移。服务层的 `due` 参数先映射到 `due_state`：`today`→`today`、`overdue`→`overdue`、`all`→`all`；边界由 `_now_naive()`（可被测试 monkeypatch）经 `business_day_bounds_utc` 计算，即 `today_start, tomorrow_start = business_day_bounds_utc(_now_naive())`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_due_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/repositories/follow_up.py app/services/follow_up.py tests/test_follow_up_due_service.py
git commit -m "feat: add due follow-up queries"
```

---

### Task 8: 附件类型识别与私有存储

**Files:**
- Create: `app/services/follow_up_attachment_storage.py`
- Test: `tests/test_follow_up_attachment_storage.py`

**Interfaces:**
- Produces:
  - `AttachmentFormat`（NamedTuple）：`content_type: str`、`extension: str`
  - `detect_attachment_format(content: bytes) -> AttachmentFormat | None`
  - `sanitize_original_name(value: str | None) -> str | None`（去控制字符与路径分隔符，取 basename，超 255 截断；空白/纯分隔符返回 `None`）
  - `StoredAttachment`（NamedTuple）：`storage_path: str`、`content_type: str`、`size_bytes: int`
  - `async def store_attachment(*, follow_up_id: int, content_type: str | None, chunks: AsyncIterator[bytes]) -> StoredAttachment`
  - `async def remove_attachment_file(storage_path: str) -> bool`
  - `def resolve_attachment_path(storage_path: str) -> Path | None`（路径非法或逃出根目录时返回 `None`）
- 支持格式与 Content-Type 严格按 spec 6.6：PNG / JPEG / WebP / PDF / DOCX / XLSX；ZIP 但不是 OOXML、或声明类型不匹配 → `BusinessError(ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID, "Attachment is invalid or unsupported", http_status=422)`；超过 `settings.follow_up_attachment_max_bytes` 同样 7020。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_attachment_storage.py
import io
import zipfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.services import follow_up_attachment_storage as storage

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


def _ooxml(prefix: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{prefix}/document.xml", "<doc/>")
    return buffer.getvalue()


DOCX = _ooxml("word")
XLSX = _ooxml("xl")


@pytest.mark.parametrize(
    ("content", "content_type", "extension"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"rest", "image/png", ".png"),
        (b"\xff\xd8\xff\xe0" + b"rest", "image/jpeg", ".jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", ".webp"),
        (PDF, "application/pdf", ".pdf"),
        (
            DOCX,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".docx",
        ),
        (
            XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
    ],
)
def test_detect_supported_formats(content, content_type, extension) -> None:
    detected = storage.detect_attachment_format(content)
    assert detected is not None
    assert detected.content_type == content_type
    assert detected.extension == extension


def test_plain_zip_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    assert storage.detect_attachment_format(buffer.getvalue()) is None


def test_sanitize_original_name_strips_paths_and_controls() -> None:
    assert storage.sanitize_original_name("../../etc/passwd") == "passwd"
    assert storage.sanitize_original_name("报价单\r\n.pdf") == "报价单.pdf"
    assert storage.sanitize_original_name("   ") is None
    assert storage.sanitize_original_name(None) is None
    assert storage.sanitize_original_name("x" * 400) == "x" * 255


def test_resolve_attachment_path_rejects_escapes(tmp_path, monkeypatch) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))
    root.mkdir()

    inside = root / "contact-follow-up" / "1" / "a.pdf"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(PDF)

    assert storage.resolve_attachment_path("contact-follow-up/1/a.pdf") == inside
    assert storage.resolve_attachment_path("../media/a.pdf") is None
    assert storage.resolve_attachment_path("/etc/passwd") is None
    assert storage.resolve_attachment_path("contact-follow-up/../../x") is None


async def test_store_attachment_writes_random_name(tmp_path, monkeypatch) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))

    async def chunks():
        yield PDF

    stored = await storage.store_attachment(
        follow_up_id=7, content_type="application/pdf", chunks=chunks()
    )
    path = storage.resolve_attachment_path(stored.storage_path)
    assert path is not None
    assert path.parent == root / "contact-follow-up" / "7"
    assert path.read_bytes() == PDF
    assert stored.content_type == "application/pdf"
    assert stored.size_bytes == len(PDF)
    assert Path(stored.storage_path).name != "document.pdf"
    assert await storage.remove_attachment_file(stored.storage_path) is True


async def test_store_attachment_rejects_declared_type_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "a"))

    async def chunks():
        yield PDF

    with pytest.raises(BusinessError) as error:
        await storage.store_attachment(
            follow_up_id=1, content_type="image/png", chunks=chunks()
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID


async def test_store_attachment_enforces_size_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "a"))
    monkeypatch.setattr(settings, "follow_up_attachment_max_bytes", 10)

    async def chunks():
        yield PDF

    with pytest.raises(BusinessError) as error:
        await storage.store_attachment(
            follow_up_id=1, content_type="application/pdf", chunks=chunks()
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_attachment_storage.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

复制 `app/services/system_setting_asset.py` 的 `_read_bounded`、`_write_atomic` 思路（本模块独立实现，不复用其私有函数）。OOXML 判定：`zipfile.ZipFile(io.BytesIO(content))`，读取 `namelist()`，要求包含 `[Content_Types].xml` 且存在以 `word/` 或 `xl/` 开头的条目；`BadZipFile` 与其它异常一律视为不支持。写入路径 `<root>/contact-follow-up/<follow_up_id>/<uuid><ext>`，`store_attachment` 内部 `await asyncio.to_thread(...)` 落盘。`resolve_attachment_path` 用 `Path.resolve()` 后校验 `is_relative_to(root.resolve())` 且必须是文件。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_attachment_storage.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/follow_up_attachment_storage.py tests/test_follow_up_attachment_storage.py
git commit -m "feat: add private attachment storage and type detection"
```

---

### Task 9: 附件服务（权限 + 持久化）

**Files:**
- Create: `app/services/follow_up_attachment.py`
- Test: `tests/test_follow_up_attachment_service.py`

**Interfaces:**
- Consumes: Task 6 的公开守卫（`require_visible_active_customer`、`require_active_contact`、`require_active_follow_up`、`require_follow_up_owner`，从 `app.services.follow_up` 导入）、Task 4 的附件仓储函数（`count_attachments`、`get_attachment`、`add_attachment`、`delete_attachment_row`）、Task 8 的 storage。
- Produces:
  - `async def upload_attachment(session, customer_id: int, contact_id: int, follow_up_id: int, *, content_type: str | None, original_name: str | None, chunks: AsyncIterator[bytes], context: AuthContext) -> ContactFollowUpAttachment`
  - `async def get_attachment_for_download(session, customer_id, contact_id, follow_up_id, attachment_id, *, context) -> tuple[Path, ContactFollowUpAttachment]`
  - `async def delete_attachment(session, customer_id, contact_id, follow_up_id, attachment_id, *, context) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_attachment_service.py
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.services import follow_up as follow_up_service
from app.services import follow_up_attachment as attachment_service
from app.services import follow_up_attachment_storage as storage
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


async def _fixture(session, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "attachments"))
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    follow_up = await add_follow_up(
        session, contact, method, follow_up_user=owner, content="带附件"
    )
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    return owner, customer, contact, follow_up, ctx


async def _upload_pdf(session, customer, contact, follow_up, ctx, *, name="报价单.pdf"):
    async def chunks():
        yield PDF

    return await attachment_service.upload_attachment(
        session,
        customer.id,
        contact.id,
        follow_up.id,
        content_type="application/pdf",
        original_name=name,
        chunks=chunks(),
        context=ctx,
    )


async def test_upload_and_download_round_trip(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(session, tmp_path, monkeypatch)
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)

    assert attachment.original_name == "报价单.pdf"
    assert attachment.size_bytes == len(PDF)
    assert attachment.content_type == "application/pdf"

    path, loaded = await attachment_service.get_attachment_for_download(
        session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
    )
    assert path.read_bytes() == PDF
    assert loaded.id == attachment.id


async def test_sixth_attachment_is_rejected(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(session, tmp_path, monkeypatch)
    for index in range(5):
        await _upload_pdf(session, customer, contact, follow_up, ctx, name=f"{index}.pdf")

    with pytest.raises(BusinessError) as error:
        await _upload_pdf(session, customer, contact, follow_up, ctx, name="6.pdf")
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_LIMIT


async def test_non_owner_cannot_upload_or_delete(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, _ = await _fixture(session, tmp_path, monkeypatch)
    outsider = await make_user(session)
    ctx = make_context(
        outsider,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )

    async def chunks():
        yield PDF

    with pytest.raises(BusinessError) as error:
        await attachment_service.upload_attachment(
            session,
            customer.id,
            contact.id,
            follow_up.id,
            content_type="application/pdf",
            original_name=None,
            chunks=chunks(),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER


async def test_deleted_attachment_file_is_gone(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(session, tmp_path, monkeypatch)
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)
    path = storage.resolve_attachment_path(attachment.storage_path)
    assert path is not None and path.exists()

    await attachment_service.delete_attachment(
        session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
    )
    assert not path.exists()
    with pytest.raises(BusinessError) as error:
        await attachment_service.get_attachment_for_download(
            session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND


async def test_tampered_storage_path_cannot_escape_root(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(session, tmp_path, monkeypatch)
    media_root = tmp_path / "media"
    media_root.mkdir()
    secret = media_root / "secret.pdf"
    secret.write_bytes(PDF)
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)
    attachment.storage_path = "../media/secret.pdf"
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await attachment_service.get_attachment_for_download(
            session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND


async def test_deleted_record_hides_attachments(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(session, tmp_path, monkeypatch)
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)

    await follow_up_service.delete_follow_up(
        session, customer.id, contact.id, follow_up.id, context=ctx
    )
    with pytest.raises(BusinessError) as error:
        await attachment_service.get_attachment_for_download(
            session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_FOUND
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_attachment_service.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`upload_attachment`：依次 `require_visible_active_customer` → `require_active_contact` → `require_active_follow_up` → `require_follow_up_owner`；`lock_follow_up_for_update` 后在锁内统计附件数（≥`settings.follow_up_attachment_max_per_record` 抛 `7019`）；调用 `store_attachment` 落盘；写入附件行并 commit；commit 失败时 `run_best_effort` 删除已写文件（照 `app/services/user_avatar.py` 的写法）；成功后审计（`@audited(action="contact_follow_up:attachment_upload", resource_type="contact_follow_up_attachment", resource_id_attr="id")`）。

`get_attachment_for_download`：依次校验客户、对接人、记录（`7013`）、附件（`7018`），`resolve_attachment_path` 返回 `None` 或文件不存在也抛 `7018`（**不区分**「路径非法」与「不存在」，避免信息泄露）。

`delete_attachment`：同样校验并对记录加锁；删除行、commit 后 `run_best_effort` 删除文件；审计 `contact_follow_up:attachment_delete`（`resource_id_param="attachment_id"`）。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_attachment_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/follow_up_attachment.py tests/test_follow_up_attachment_service.py
git commit -m "feat: add follow-up attachment service"
```

---

### Task 10: 接口层

**Files:**
- Modify: `app/schemas/follow_up.py`（追加响应模型）
- Create: `app/api/follow_ups.py`
- Modify: `app/api/router.py`
- Modify: `app/main.py`（`OPENAPI_TAGS` 增加「客户跟进」）
- Modify: `tests/test_app_boot.py`（tag 断言增加「客户跟进」）
- Test: `tests/test_follow_up_api.py`

**Interfaces:**
- Produces（响应模型，均 `model_config = ConfigDict(from_attributes=True)` 或用构造函数显式赋值）：
  - `FollowUpMethodRead`：`id`、`name`、`created_at`、`updated_at`
  - `FollowUpAttachmentRead`：`id`、`original_name: str | None`、`content_type`、`size_bytes`、`created_at`
  - `FollowUpRead`：`id`、`customer_id`、`contact_id`、`contact_name`、`method_id`、`method_name`、`follow_up_user_id`、`follow_up_user_name`、`created_by`、`created_by_name`、`follow_up_at`、`content`、`next_follow_up_at`、`attachments: list[FollowUpAttachmentRead]`、`created_at`、`updated_at`
  - `DueFollowUpRead`：`follow_up_id`、`due_state: Literal["today", "overdue"]`、`next_follow_up_at`、`follow_up_at`、`customer_id`、`customer_name`、`contact_id`、`contact_name`、`function_name`、`follow_up_user_id`、`follow_up_user_name`
  - `DueCustomerRead`：`customer_id`、`customer_name`、`due_count`、`overdue_count`、`earliest_next_follow_up_at`
  - `DueSummaryRead`：`today: int`、`overdue: int`
- 时间字段统一加 `@field_serializer(..., when_used="json")` 调用 `_serialize_utc` 的等价实现（在 `app/schemas/follow_up.py` 内复制 `_serialize_utc`，与 `schemas/customer.py` 保持一致）。
- 路由与权限（`app/api/follow_ups.py`，`router = APIRouter(tags=["客户跟进"])`）：
  - `GET /follow-up-methods` → `customer:read`
  - `POST /follow-up-methods` → 超级管理员（依赖只校验登录；服务层校验身份）
  - `GET /follow-up-methods/recycle-bin` → 超级管理员
  - `PATCH /follow-up-methods/{method_id}`、`DELETE /follow-up-methods/{method_id}`、`POST /follow-up-methods/{method_id}/restore` → 超级管理员
  - `GET /customers/{customer_id}/contacts/{contact_id}/follow-ups` → `customer:read`
  - `POST .../follow-ups` → `customer:update`
  - `GET .../follow-ups/recycle-bin` → `customer:delete`
  - `PATCH .../follow-ups/{follow_up_id}` → `customer:update`
  - `DELETE .../follow-ups/{follow_up_id}`、`POST .../follow-ups/{follow_up_id}/restore` → `customer:delete`
  - `POST .../follow-ups/{follow_up_id}/attachments` → `customer:update`，原始体 + `X-File-Name` 头
  - `GET .../follow-ups/{follow_up_id}/attachments/{attachment_id}/download` → `customer:read`，返回 `FileResponse(path, media_type=attachment.content_type, filename=original_name or f"attachment-{id}{ext}", headers={"Cache-Control": "no-store"})`
  - `DELETE .../follow-ups/{follow_up_id}/attachments/{attachment_id}` → `customer:update`
  - `GET /follow-ups/due`、`GET /follow-ups/due/customers`、`GET /follow-ups/due/summary` → `customer:read`
- 超级管理员路由的写法：`dependencies=[Depends(require_permission("customer:read"))]` 不合适（会放宽到普通用户再被服务层拒），照 `app/api/contacts.py` 中共用职能路由的既有做法——只依赖 `get_current_context`，由服务层 `_require_super_admin` 抛 403。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_api.py
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from app.core.audit_meta import get_audit_route_meta
from app.models.contact import ContactStatus
from app.services import audit as audit_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import api_client, make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


@pytest_asyncio.fixture(autouse=True)
async def disable_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_service, "record_audit_entry", noop)


async def _fixture(session):  # noqa: F811
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function, name="张三")
    method = await add_method(session, name="电话")
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
        is_super_admin=True,
    )
    return owner, customer, contact, method, ctx


async def test_create_and_list_records(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    async with api_client(session, ctx) as client:
        created = await client.post(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups",
            json={
                "method_id": method.id,
                "content": "电话沟通二期预算",
                "follow_up_at": "2026-10-18T06:30:00+00:00",
                "next_follow_up_at": "2026-10-21T02:00:00+00:00",
            },
        )
        assert created.status_code == 200
        body = created.json()["data"]
        assert body["method_name"] == "电话"
        assert body["contact_name"] == "张三"
        assert body["follow_up_user_id"] == owner.id
        assert body["follow_up_at"] == "2026-10-18T06:30:00+00:00"

        listed = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups"
        )
        data = listed.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == body["id"]


async def test_non_owner_gets_7017_from_api(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    author = await make_user(session)
    record = await add_follow_up(
        session, contact, method, follow_up_user=author, content="别人的"
    )
    ctx = make_context(owner, permission_codes={"customer:read", "customer:update", "customer:delete"})
    async with api_client(session, ctx) as client:
        response = await client.patch(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups/{record.id}",
            json={"content": "改"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == 7017


async def test_routes_require_permissions(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    ctx = make_context(owner)  # 无任何权限码
    async with api_client(session, ctx) as client:
        for method_name, url in (
            ("GET", f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups"),
            ("POST", f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups"),
            ("GET", "/api/v1/follow-ups/due"),
            ("GET", "/api/v1/follow-up-methods"),
        ):
            response = await client.request(
                method_name,
                url,
                json={"method_id": method.id, "content": "x"},
            )
            assert response.status_code == 403, url


async def test_method_route_denies_non_super_admin(session) -> None:  # noqa: F811
    owner, _, _, _, _ = await _fixture(session)
    ctx = make_context(owner, permission_codes={"customer:read", "customer:update"})
    async with api_client(session, ctx) as client:
        response = await client.post("/api/v1/follow-up-methods", json={"name": "钉钉"})
    assert response.status_code == 403


async def test_due_summary_and_customers_endpoints(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        follow_up_at=datetime(2026, 10, 17, 16, 0),
        next_follow_up_at=datetime(2026, 10, 17, 15, 0),
    )
    monkeypatch.setattr(
        "app.services.follow_up._now_naive",
        lambda: datetime(2026, 10, 17, 20, 0),
    )
    async with api_client(session, ctx) as client:
        summary = await client.get("/api/v1/follow-ups/due/summary")
        assert summary.json()["data"] == {"today": 0, "overdue": 1}

        customers = await client.get("/api/v1/follow-ups/due/customers")
        items = customers.json()["data"]["items"]
        assert items[0]["customer_id"] == customer.id
        assert items[0]["due_count"] == 1
        assert items[0]["overdue_count"] == 1


async def test_attachment_upload_and_download(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "attachments"))
    owner, customer, contact, method, ctx = await _fixture(session)
    record = await add_follow_up(session, contact, method, follow_up_user=owner, content="附件")
    pdf = b"%PDF-1.7\n%%EOF\n"
    async with api_client(session, ctx) as client:
        upload = await client.post(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}/attachments",
            content=pdf,
            headers={
                "Content-Type": "application/pdf",
                "X-File-Name": "%E6%8A%A5%E4%BB%B7%E5%8D%95.pdf",
            },
        )
        assert upload.status_code == 200
        attachment = upload.json()["data"]
        assert attachment["original_name"] == "报价单.pdf"

        download = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}/attachments/{attachment['id']}/download"
        )
        assert download.status_code == 200
        assert download.content == pdf
        assert "attachment" in download.headers["content-disposition"]
        assert download.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "template", "action", "resource_type", "resource_id_param"),
    [
        ("POST", "/follow-up-methods", "follow_up_method:create", "contact_follow_up_method", None),
        ("PATCH", "/follow-up-methods/{method_id}", "follow_up_method:update", "contact_follow_up_method", "method_id"),
        ("DELETE", "/follow-up-methods/{method_id}", "follow_up_method:delete", "contact_follow_up_method", "method_id"),
        ("POST", "/follow-up-methods/{method_id}/restore", "follow_up_method:restore", "contact_follow_up_method", "method_id"),
        ("POST", "/customers/{customer_id}/contacts/{contact_id}/follow-ups", "contact_follow_up:create", "contact_follow_up", None),
        ("PATCH", "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}", "contact_follow_up:update", "contact_follow_up", "follow_up_id"),
        ("DELETE", "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}", "contact_follow_up:delete", "contact_follow_up", "follow_up_id"),
        ("POST", "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/restore", "contact_follow_up:restore", "contact_follow_up", "follow_up_id"),
        ("POST", "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/attachments", "contact_follow_up:attachment_upload", "contact_follow_up_attachment", None),
        ("DELETE", "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}/attachments/{attachment_id}", "contact_follow_up:attachment_delete", "contact_follow_up_attachment", "attachment_id"),
    ],
)
def test_write_routes_are_registered_for_audit(
    method, template, action, resource_type, resource_id_param
) -> None:
    meta = get_audit_route_meta(method, template)
    assert meta is not None
    assert meta.action == action
    assert meta.resource_type == resource_type
    assert meta.resource_id_param == resource_id_param
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_api.py -v`
Expected: FAIL（路由不存在，404）

- [ ] **Step 3: 实现**

`app/schemas/follow_up.py` 追加响应模型；`FollowUpRead` 由 API 层用构造函数组装（`FollowUpRow` + `follow_up_repository.list_attachments(session, [row.follow_up.id for row in rows])` 的批量结果 + `display_name`）。`display_name` 复用 `app/services/directory.py` 的同名函数（`from app.services.directory import display_name`）。记录详情不复用列表行的组装函数时，也要按 `follow_up_id` 取附件（`list_attachments(session, [follow_up_id])`），避免单条查询另写一份。

`app/api/follow_ups.py` 按 `app/api/contacts.py` 的写法（无 router prefix，路径写全）。下载接口用 `FileResponse`。上传接口读 `request.headers.get("content-type")` 与 `request.headers.get("x-file-name")`（后者 `urllib.parse.unquote`，失败/为空传 `None`）。

`app/api/router.py` 增加 `follow_ups` 路由；`app/main.py` 的 `OPENAPI_TAGS` 增加 `{"name": "客户跟进", "description": "对接人跟进记录、跟进方式与待跟进清单"}`；`tests/test_app_boot.py` 的期望集合增加 `"客户跟进"`。

写路由末尾注册全部 `register_audit_route(...)`（参数见测试参数表）。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_api.py tests/test_app_boot.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/schemas/follow_up.py app/api/follow_ups.py app/api/router.py app/main.py tests/test_app_boot.py tests/test_follow_up_api.py
git commit -m "feat: expose follow-up HTTP API"
```

---

### Task 11: 初始跟进方式

**Files:**
- Modify: `scripts/init_data.py`
- Test: `tests/test_follow_up_seed.py`

**Interfaces:**
- Produces: `scripts.init_data._ensure_follow_up_methods(session) -> None`，幂等写入 电话、微信、拜访、邮件，并在 `init_data(...)` 中于 `_ensure_shared_contact_functions` 之后调用。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_follow_up_seed.py
from sqlalchemy import select

from app.models.contact import ContactStatus
from app.models.follow_up import ContactFollowUpMethod
from scripts.init_data import _ensure_follow_up_methods
from tests.customer_helpers import session  # noqa: F401


async def test_follow_up_methods_are_seeded_once(session) -> None:  # noqa: F811
    await _ensure_follow_up_methods(session)
    await _ensure_follow_up_methods(session)

    rows = (await session.scalars(select(ContactFollowUpMethod))).all()
    assert sorted(row.name for row in rows) == ["微信", "拜访", "电话", "邮件"]
    assert all(row.status == ContactStatus.ACTIVE for row in rows)
    assert all(row.active_name == row.name for row in rows)


async def test_existing_active_method_is_not_duplicated(session) -> None:  # noqa: F811
    session.add(
        ContactFollowUpMethod(name="电话", status=ContactStatus.ACTIVE)
    )
    await session.flush()

    await _ensure_follow_up_methods(session)

    rows = (await session.scalars(select(ContactFollowUpMethod))).all()
    assert len(rows) == 4
    assert len([row for row in rows if row.name == "电话"]) == 1


async def test_deleted_method_is_not_restored_by_seed(session) -> None:  # noqa: F811
    session.add(
        ContactFollowUpMethod(name="电话", status=ContactStatus.DELETED)
    )
    await session.flush()

    await _ensure_follow_up_methods(session)

    rows = (await session.scalars(select(ContactFollowUpMethod))).all()
    assert len([row for row in rows if row.name == "电话"]) == 1
    assert len([row for row in rows if row.status == ContactStatus.ACTIVE]) == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_follow_up_seed.py -v`
Expected: FAIL（`_ensure_follow_up_methods` 不存在）

- [ ] **Step 3: 实现**

照 `_ensure_shared_contact_functions` 的写法；按 `status == ACTIVE` 且同名判断是否已存在（已删除的不算，也不复活——与共用职能的种子语义一致）。在 `init_data` 中新增调用。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_follow_up_seed.py tests/test_seed_data.py tests/test_contact_seed.py -v`
Expected: PASS（`EXPECTED_PERMISSION_CODES` 不变）

- [ ] **Step 5: 提交**

```bash
git add scripts/init_data.py tests/test_follow_up_seed.py
git commit -m "feat: seed initial follow-up methods"
```

---

### Task 12: 全量回归与代码质量

**Files:** 无新增。

- [ ] **Step 1: 跑全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿；若既有测试因新增 OpenAPI tag 或配置字段失败，按本计划已声明的改动修正（仅限 `tests/test_app_boot.py`、`tests/test_config.py`）。

- [ ] **Step 2: 静态检查**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy app`
Expected: 三条均无错误。mypy strict 下注意 `to_utc_naive` 返回 `datetime | None`，`business_day_bounds_utc` 内需显式断言非空。

- [ ] **Step 3: 迁移往返**

Run: `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic downgrade base && JWT_SECRET=x CONFIG_ENCRYPTION_KEY=x .venv/bin/alembic upgrade head`
Expected: 成功（在测试库上执行）

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "test: verify follow-up feature regression"
```

---

## Self-Review

- **Spec 覆盖**：6.1 方式 → Task 2/5/11；6.2 记录 → Task 2/6；6.3 附件 → Task 2/8/9；6.4 权限 → Task 5/6/9/10；6.5 软删除与唯一性 → Task 2/5；6.6 附件识别与私有目录 → Task 3/8；6.7 待跟进口径 → Task 1/7；6.8 并发 → Task 5/9（行锁）；7 接口 → Task 10；8 流程 → 各任务实现步骤；9 错误码 → Task 1；11 审计 → Task 5/6/9/10；12 测试 → 各任务；13 迁移/配置/种子 → Task 2/3/11。非目标与反目标无对应任务，符合预期。
- **类型一致性**：`FollowUpRow`、`DueFollowUpRow`、`DueCustomerRow`、`StoredAttachment`、`AttachmentFormat` 在定义任务与消费任务中名称、字段一致；`display_name(nickname, username, user_id)` 复用 `app/services/directory.py` 的既有签名。
- **Review Focus 落点**：时区边界 → Task 7 `test_due_boundaries`；跨客户 ID → Task 6 `test_foreign_customer_contact_pair_returns_7005`；大页码 → Task 7 `test_large_page_returns_empty`；路径篡改 → Task 9 `test_tampered_storage_path_cannot_escape_root`；离职后只读 → Task 6 `test_record_of_soft_deleted_user_stays_readable_but_read_only`。
