# 客户对接人与职能 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为客户增加对接人与职能（共用职能 + 客户自定义职能），并将客户、职能、对接人统一改为软删除（`status` 标识，支持恢复）。

**Architecture:** 新增两张表 `contact_function`、`customer_contact`，客户表增加软删除字段与生成列唯一键。职能与对接人的业务逻辑放在 `app/services/contact.py`，客户软删除、恢复、转移留在 `app/services/customer.py`。接口挂在 `/api/v1/customers/{id}/...` 与 `/api/v1/contact-functions`。权限复用 `customer:read` / `customer:update` / `customer:delete`，共用职能维护仅校验 `ctx.is_super_admin`。

**Tech Stack:** FastAPI、SQLAlchemy 2.1（已验证生成列 DDL：`GENERATED ALWAYS AS ... STORED` 且唯一约束生效）、asyncmy（MySQL 8）、Alembic、pytest + pytest-asyncio（`asyncio_mode = "auto"`）。

**Spec:** `docs/superpowers/specs/2026-10-17-customer-contacts-design.md`（已批准）。执行者必须同时阅读 spec。

## Global Constraints

- 迁移 `down_revision` 固定为 `cf349e47acee`（当前唯一 head）。
- 不新增权限码。`tests/test_seed_data.py` 中 `EXPECTED_PERMISSION_CODES` 保持不变。
- 错误码：`CUSTOMER_CONTACT_NOT_FOUND = 7005`、`CONTACT_FUNCTION_NOT_FOUND = 7006`、`CONTACT_FUNCTION_NAME_EXISTS = 7007`、`CONTACT_FUNCTION_REPLACEMENT_REQUIRED = 7008`、`CONTACT_FUNCTION_REPLACEMENT_INVALID = 7009`、`CONTACT_FUNCTION_UNAVAILABLE = 7010`、`CUSTOMER_DELETED_CREDIT_CODE_EXISTS = 7011`、`RECORD_NOT_DELETED = 7012`。字段校验沿用 `CUSTOMER_INVALID_FIELD = 7003`。
- 软删除统一字段：`status`（`ACTIVE` / `DELETED`）、`deleted_at`、`deleted_by`。列表与唯一性只针对 `ACTIVE`。
- 分页沿用 `app/core/response.py` 的 `Page` 与 `MAX_PAGE_SIZE = 100`。
- 审计不新增 scope 字段，`resource_type` 为 `customer_contact` / `contact_function`。
- 测试库：`mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test`，Redis `localhost:6379`。`tests/conftest.py` 自动设置 `CONFIG_ENCRYPTION_KEY`。运行 alembic 命令时需显式设置 `JWT_SECRET` 与 `CONFIG_ENCRYPTION_KEY`。

## Review Focus

以下情形最容易在真实使用中出问题，每条都有测试绑定到对应任务：

1. 已删除客户的信用代码再次录入：返回 `7011`，不调用天眼查，不附 ID。（Task 3）
2. 删除职能与并发新增对接人同时发生：不得留下指向已删除职能的对接人。（Task 7）
3. 删除用户时名下存在已删除客户：阻止删除（`2005`），转移后可删除。（Task 3）
4. 手机号、邮箱为空字符串或仅含空白：视为未填写，不做格式校验。（Task 4）
5. 分页 `page` 很大时返回空列表而不是 500。（Task 8）

---

## File Structure

- Create: `app/models/contact.py`：`ContactStatus`、`ContactFunction`、`CustomerContact`。
- Modify: `app/models/customer.py`：增加 `status`、`deleted_at`、`deleted_by`、`active_credit_code`（生成列）；去掉 `credit_code` 上的唯一索引。
- Create: `migrations/versions/<rev>_customer_contacts.py`。
- Create: `app/core/errors.py` 修改：新增错误码。
- Create: `app/services/contact_validation.py`：电话与邮箱格式（纯函数）。
- Create: `app/repositories/contact.py`：职能与对接人的数据访问。
- Modify: `app/repositories/customer.py`：软删除、恢复、按状态查询、`get_by_credit_code_any_status`。
- Create: `app/services/contact.py`：职能与对接人业务逻辑。
- Modify: `app/services/customer.py`：软删除、恢复、回收站、信用代码分支、转移权限。
- Create: `app/schemas/contact.py`、`app/api/contacts.py`。
- Modify: `app/api/customers.py`、`app/api/router.py`。
- Modify: `scripts/init_data.py`：幂等写入共用职能。
- Create: `tests/test_contact_*.py`、`tests/test_customer_soft_delete.py`。
- Modify: `tests/test_customer_repository.py`、`tests/test_customer_e2e.py`（硬删除断言改为软删除语义）。

**共享测试 helper（签名已固定，所有任务只能从这里导入，不得各自复制）：**

`tests/customer_helpers.py`（已创建，冒烟测试 `tests/test_customer_helpers_smoke.py` 通过）：

- `session` — pytest fixture，单事务会话，测试结束回滚。
- `make_department(session, *, name=None, is_system=False) -> Department`
- `make_user(session, *, department=None, status=UserStatus.ACTIVE) -> User`
- `make_context(user, *, permission_codes: set[str] | None = None, data_scope=DataScope.ALL, is_super_admin=False, role_id=1) -> AuthContext`
- `make_customer_row(session, *, owner, credit_code=None, name=None, source=CustomerSource.MANUAL) -> Customer`
- `api_client(session, context, fake_redis=None)` — 异步上下文管理器，返回 `httpx.AsyncClient`。
- `FakeTianyanchaClient` — 记录调用的替身；`fake_client` 的注入方式与 `tests/test_customer_service.py` 现有写法一致。
- `REQUESTED_CREDIT_CODE`、`TEST_DATABASE_URL`。

`tests/contact_helpers.py`（Task 4 中创建并验证，依赖 Task 2 的模型）：

- `add_function(session, *, name, customer_id, status=ContactStatus.ACTIVE) -> ContactFunction`：`customer_id=None` 为共用职能（`scope_key=0`），否则为自定义（`scope_key=customer_id`）。
- `add_contact(session, customer, function, *, name=None, status=ContactStatus.ACTIVE) -> CustomerContact`。

**迁移现有测试（Task 3 中执行）：** `tests/test_customer_service.py` 的 `session`、`make_user`、`make_context`、`make_customer_row`、`FakeTianyanchaClient` 改为从 `tests/customer_helpers.py` 导入，删除本地副本。`make_context` 的旧签名（`data_scope`、`is_super_admin`、`role_id`）与新签名兼容，无需改调用点。运行 `.venv/bin/pytest tests/test_customer_service.py -q` 必须全部通过。

---

### Task 1: 错误码与状态枚举

**Files:**
- Modify: `app/core/errors.py`
- Create: `app/models/contact.py`（本任务只含枚举）
- Test: `tests/test_contact_error_codes.py`

**Interfaces:**
- Produces: `ErrorCode` 新增 8 个成员（见 Global Constraints）；`ContactStatus(StrEnum)`，`ACTIVE = "ACTIVE"`、`DELETED = "DELETED"`，位于 `app.models.contact`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contact_error_codes.py
from app.core.errors import ErrorCode
from app.models.contact import ContactStatus


def test_contact_error_codes_match_spec() -> None:
    assert ErrorCode.CUSTOMER_CONTACT_NOT_FOUND == 7005
    assert ErrorCode.CONTACT_FUNCTION_NOT_FOUND == 7006
    assert ErrorCode.CONTACT_FUNCTION_NAME_EXISTS == 7007
    assert ErrorCode.CONTACT_FUNCTION_REPLACEMENT_REQUIRED == 7008
    assert ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID == 7009
    assert ErrorCode.CONTACT_FUNCTION_UNAVAILABLE == 7010
    assert ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS == 7011
    assert ErrorCode.RECORD_NOT_DELETED == 7012


def test_contact_status_values() -> None:
    assert ContactStatus.ACTIVE == "ACTIVE"
    assert ContactStatus.DELETED == "DELETED"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_error_codes.py -v`
Expected: FAIL（`ErrorCode` 无该成员，`app.models.contact` 不存在）

- [ ] **Step 3: 实现**

在 `app/core/errors.py` 的 `CUSTOMER_OWNER_UNAVAILABLE = 7004` 之后追加 7005–7012 成员，名称与数值见 Global Constraints。

创建 `app/models/contact.py`：

```python
from enum import StrEnum


class ContactStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_error_codes.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add app/core/errors.py app/models/contact.py tests/test_contact_error_codes.py
git commit -m "feat: add contact error codes and status enum"
```

---

### Task 2: 数据模型与迁移

**Files:**
- Modify: `app/models/customer.py`
- Modify: `app/models/contact.py`（追加两个模型）
- Create: `migrations/versions/<rev>_customer_contacts.py`（`<rev>` 由 alembic 生成）
- Test: `tests/test_contact_models.py`

**Interfaces:**
- Produces:
  - `Customer` 新增 `status: Mapped[ContactStatus]`（默认 `ACTIVE`，`server_default='ACTIVE'`）、`deleted_at: Mapped[datetime | None]`（`DATETIME(fsp=6)`）、`deleted_by: Mapped[int | None]`（`BigInteger`）、`active_credit_code: Mapped[str | None]`（生成列，`Computed("CASE WHEN status = 'ACTIVE' THEN credit_code END", persisted=True)`，唯一索引 `ix_customer_active_credit_code`）。`credit_code` 去掉 `unique=True` 与 `index=True`。
  - `ContactFunction`（表 `contact_function`）：`id`、`name: String(64)`、`customer_id: BigInteger | None`（FK `customer.id` RESTRICT）、`scope_key: BigInteger`（非空；共用为 `0`，自定义为 `customer_id`）、`status`、`deleted_at`、`deleted_by`、`active_name`（生成列 `CASE WHEN status = 'ACTIVE' THEN name END`，persisted）、`created_by`、`updated_by`、`created_at`、`updated_at`。唯一约束 `uq_contact_function_scope_name` on `(scope_key, active_name)`。
  - `CustomerContact`（表 `customer_contact`）：`id`、`customer_id`（FK RESTRICT，建索引）、`function_id`（FK `contact_function.id` RESTRICT，建索引）、`name: String(255)`、`position: String(128) | None`、`phone: String(32) | None`、`email: String(255) | None`、`home_address: String(512) | None`、`remark: Text | None`、`status`、`deleted_at`、`deleted_by`、`created_at`、`updated_at`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contact_models.py
from sqlalchemy import inspect

from app.models.contact import ContactFunction, CustomerContact
from app.models.customer import Customer


def test_customer_has_soft_delete_columns() -> None:
    columns = {c.name for c in inspect(Customer).columns}
    assert {"status", "deleted_at", "deleted_by", "active_credit_code"} <= columns


def test_customer_credit_code_unique_is_only_on_active_column() -> None:
    unique_cols = {
        tuple(c.name for c in index.columns)
        for index in Customer.__table__.indexes
        if index.unique
    }
    assert ("active_credit_code",) in unique_cols
    assert ("credit_code",) not in unique_cols


def test_contact_function_columns() -> None:
    cols = {c.name for c in inspect(ContactFunction).columns}
    assert {"name", "customer_id", "scope_key", "status", "active_name"} <= cols


def test_customer_contact_columns() -> None:
    cols = {c.name for c in inspect(CustomerContact).columns}
    assert {
        "customer_id", "function_id", "name", "position", "phone",
        "email", "home_address", "remark", "status",
    } <= cols
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_models.py -v`
Expected: FAIL（模型不存在）

- [ ] **Step 3: 实现模型**

- `Customer` 与 `ContactFunction` 的生成列用 `mapped_column(..., Computed(..., persisted=True))` 声明；Task 2 已在临时模型上验证该写法能生成 `STORED` 列并使唯一约束生效。
- `customer.py` 从 `app.models.contact` 导入 `ContactStatus`；单向依赖，`contact.py` 不得反向导入 `customer.py` 的模块级对象（外键用字符串 `"customer.id"`）。
- `ondelete` 全部为 `RESTRICT`。

- [ ] **Step 4: 生成并修正迁移**

Run: `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") .venv/bin/alembic revision --autogenerate -m "customer contacts"`

手工检查生成文件：
- `down_revision = "cf349e47acee"`。
- 生成列用 `sa.Computed(..., persisted=True)`。autogenerate 可能不识别生成列，此时手写 `op.add_column` 并补 `sa.Computed`。
- 删除 `ix_customer_credit_code` 唯一索引，创建 `ix_customer_active_credit_code`（唯一，作用于 `active_credit_code`）。
- 迁移的 `downgrade` 必须能回滚：先删 `customer_contact`、`contact_function`，再恢复 `customer` 的旧索引并删除新列。

- [ ] **Step 5: 迁移正反向验证**

Run: `export JWT_SECRET=x DATABASE_URL="mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test" CONFIG_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head && .venv/bin/alembic current`
Expected: 三步均成功，`current` 显示新 revision（`(head)`）。

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/bin/pytest tests/test_contact_models.py tests/test_customer_repository.py -v`
Expected: PASS。`test_customer_repository.py` 中的硬删除断言此时可能失败，留到 Task 3 修改。若失败，记录但不在本任务修复。

- [ ] **Step 7: 提交**

```bash
git add app/models migrations/versions tests/test_contact_models.py
git commit -m "feat: add contact tables and customer soft delete columns"
```

---

### Task 3: 客户软删除、恢复、已删除客户的查重与转移

**Files:**
- Create: `tests/customer_helpers.py`（共享 fixture 与 helper，已完成）
- Create: `tests/test_customer_helpers_smoke.py`（冒烟测试，已完成）
- Modify: `tests/test_customer_service.py`（改为从 `customer_helpers` 导入）
- Modify: `app/repositories/customer.py`
- Modify: `app/services/customer.py`
- Modify: `app/api/customers.py`
- Modify: `app/services/user.py`（不改逻辑，仅确认其 `has_customers` 不过滤状态）
- Modify: `tests/test_customer_repository.py`、`tests/test_customer_e2e.py`
- Test: `tests/test_customer_soft_delete.py`

**Interfaces:**
- Consumes: Task 2 的 `Customer.status`、`deleted_at`、`deleted_by`。
- Produces（`app/repositories/customer.py`）：
  - `soft_delete(session, customer, *, deleted_by: int) -> None`：设 `status=DELETED`、`deleted_at=now (UTC naive)`、`deleted_by`。**替换**原 `delete(session, customer)`，调用点同步改为 `soft_delete`。
  - `restore(session, customer) -> None`：`status=ACTIVE`，清空 `deleted_at`、`deleted_by`。
  - `get_by_credit_code_any_status(session, credit_code) -> Customer | None`：不过滤状态。
  - `list_customers(..., status: ContactStatus)`：新增参数，过滤 `Customer.status == status`。
  - `has_customers`、`has_customers_for_update`：**不加**状态过滤（spec 8.8）。
- Produces（`app/services/customer.py`）：
  - `delete_customer(session, customer_id, *, context) -> None`：`@audited(action="customer:delete", ...)`。取 ACTIVE 可见客户，软删除。
  - `restore_customer(session, customer_id, *, context) -> Customer`：`@audited(action="customer:restore", resource_type="customer", resource_id_param="customer_id")`。取可见的 DELETED 客户（不存在或不可见 → `7001`）。非 DELETED → `7012`。`credit_code` 非空且存在 ACTIVE 占用 → `7002`（`_duplicate_error`，可见时附 ID）。否则 restore，commit。
  - `list_deleted_customers(session, *, context, page, page_size) -> tuple[list[Customer], int]`。
  - `transfer_owner(session, customer_id, owner_user_id, *, context) -> Customer`：取可见客户（ACTIVE 或 DELETED 均可）。权限：若客户 ACTIVE，要求 `customer:update`；若 DELETED，要求 `customer:delete`。缺少对应权限 → `FORBIDDEN` 403。超管（`is_super_admin`）视为拥有全部权限。
- 查重分支（`create_customer`、`import_from_tianyancha`、`update_customer` 的信用代码路径）：先调 `get_by_credit_code_any_status`，命中 DELETED → `BusinessError(CUSTOMER_DELETED_CREDIT_CODE_EXISTS, "Unified social credit code belongs to a deleted customer", 409)`，**不附 `extra`**；命中 ACTIVE → 沿用 `_duplicate_error`。
- 路由（`app/api/customers.py`）：
  - `PATCH /customers/{customer_id}/owner` 的依赖由 `require_permission("customer:update")` 改为 `get_current_context`，权限在服务层按状态判定（见 transfer_owner）。
  - 新增 `GET /customers/recycle-bin`（`customer:delete`，`Page[CustomerRead]`），**声明在 `/{customer_id}` 之前**。
  - 新增 `POST /customers/{customer_id}/restore`（`customer:delete`，`CustomerRead`）。
  - `register_audit_route("POST", "/customers/{customer_id}/restore", action="customer:restore", resource_type="customer")`。

- [ ] **Step 1: 抽出测试 helper**

把 `tests/test_customer_service.py` 中的 `session` fixture、`make_user`、`make_context`、`make_customer_row`、`FakeTianyanchaClient` 与 `TEST_DATABASE_URL` 移入 `tests/customer_helpers.py`，`test_customer_service.py` 改为导入。运行 `.venv/bin/pytest tests/test_customer_service.py -q`，Expected: 行为不变（全部通过）。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_customer_soft_delete.py
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.models.customer import Customer
from app.models.user import User
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.services.customer import (
    create_customer,
    delete_customer,
    get_customer,
    import_from_tianyancha,
    list_deleted_customers,
    restore_customer,
    transfer_owner,
    update_customer,
)
from app.services.user import delete_user
from tests.conftest import FakeRedis
from tests.customer_helpers import (  # noqa: F401
    FakeTianyanchaClient,
    make_context,
    make_customer_row,
    make_user,
    session,
)

CODE = "91110000MA00000001"


async def test_delete_is_soft_and_hidden_from_get(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner)

    await delete_customer(session, customer.id, context=ctx)

    row = await session.scalar(select(Customer).where(Customer.id == customer.id))
    assert row is not None
    assert row.status == ContactStatus.DELETED
    assert row.deleted_by == owner.id
    assert row.deleted_at is not None
    with pytest.raises(BusinessError) as exc:
        await get_customer(session, customer.id, context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_restore_reactivates_and_clears_deletion_fields(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner)
    await delete_customer(session, customer.id, context=ctx)

    restored = await restore_customer(session, customer.id, context=ctx)

    assert restored.status == ContactStatus.ACTIVE
    assert restored.deleted_at is None
    assert restored.deleted_by is None


async def test_restore_active_customer_returns_7012(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    customer = await make_customer_row(session, owner=owner)

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, customer.id, context=ctx)
    assert exc.value.code == ErrorCode.RECORD_NOT_DELETED


async def test_restore_conflicts_when_active_customer_reuses_code(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)
    # 直接写库构造 ACTIVE 占用（绕过 7011 服务检查），模拟历史数据。
    await make_customer_row(session, owner=owner, credit_code=CODE)

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, deleted.id, context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_CREDIT_CODE_EXISTS
    assert exc.value.http_status == 409


async def test_create_with_deleted_credit_code_returns_7011_without_id(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session, CustomerCreate(name="新客户", credit_code=CODE), context=ctx
        )
    assert exc.value.code == ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS
    assert exc.value.extra is None


async def test_import_with_deleted_credit_code_does_not_call_tianyancha(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)

    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(session, CODE, None, context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS
    assert fake_client.calls == []


async def test_update_to_deleted_credit_code_returns_7011(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner)
    deleted = await make_customer_row(session, owner=owner, credit_code=CODE)
    await delete_customer(session, deleted.id, context=ctx)
    target = await make_customer_row(session, owner=owner)

    with pytest.raises(BusinessError) as exc:
        await update_customer(
            session, target.id, CustomerUpdate(credit_code=CODE), context=ctx
        )
    assert exc.value.code == ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS


async def test_transfer_deleted_customer_needs_delete_permission(session: AsyncSession) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    await delete_customer(
        session,
        customer.id,
        context=make_context(owner, permission_codes={"customer:delete"}),
    )

    # 只有 customer:update，无 customer:delete：转移已删除客户必须被拒绝。
    update_only = make_context(owner, permission_codes={"customer:update"})
    with pytest.raises(BusinessError) as exc:
        await transfer_owner(session, customer.id, target.id, context=update_only)
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert exc.value.http_status == 403


async def test_transfer_deleted_customer_keeps_deleted_status(session: AsyncSession) -> None:
    owner = await make_user(session)
    target = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    await delete_customer(session, customer.id, context=ctx)

    moved = await transfer_owner(session, customer.id, target.id, context=ctx)
    assert moved.owner_user_id == target.id
    assert moved.status == ContactStatus.DELETED


async def test_user_with_deleted_customer_blocked_until_transferred(
    session: AsyncSession,
    fake_redis: FakeRedis,
) -> None:
    owner = await make_user(session)
    heir = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    await delete_customer(session, customer.id, context=ctx)

    with pytest.raises(BusinessError) as exc:
        await delete_user(
            session,
            owner.id,
            context=make_context(heir, is_super_admin=True),
            redis=fake_redis,
        )
    assert exc.value.code == ErrorCode.USER_HAS_CUSTOMERS

    await transfer_owner(
        session,
        customer.id,
        heir.id,
        context=make_context(owner, permission_codes={"customer:delete"}),
    )
    await delete_user(
        session,
        owner.id,
        context=make_context(heir, is_super_admin=True),
        redis=fake_redis,
    )
    remaining = await session.scalar(select(User).where(User.id == owner.id))
    assert remaining is None


async def test_recycle_bin_lists_only_deleted_within_scope(session: AsyncSession) -> None:
    owner = await make_user(session)
    ctx = make_context(owner, permission_codes={"customer:delete"})
    active = await make_customer_row(session, owner=owner)
    deleted = await make_customer_row(session, owner=owner)
    await delete_customer(session, deleted.id, context=ctx)

    items, total = await list_deleted_customers(session, context=ctx, page=1, page_size=20)
    assert total == 1
    assert [c.id for c in items] == [deleted.id]
    assert active.id not in [c.id for c in items]
```

注：`make_context` 已支持 `permission_codes` 与 `is_super_admin` 关键字参数（见 Task 3 共享 helper 签名）。`fake_client` 为 `tests/test_customer_service.py` 现有的 fixture，迁移后由 `tests/customer_helpers.py` 的 `FakeTianyanchaClient` 提供；函数签名中的 `...` 指 `session` 与所需 fixture，实现时只写 `session` 与 `fake_client`（若该测试需要）。

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_customer_soft_delete.py -v`
Expected: FAIL（`restore_customer`、`list_deleted_customers` 不存在；删除仍为硬删除）

- [ ] **Step 4: 实现仓储与服务**

按 Interfaces 实现。注意：
- `_get_scoped_customer` 增加 `status: ContactStatus = ContactStatus.ACTIVE` 参数，并在 `apply_customer_scope` 之后追加 `Customer.status == status`。
- `delete_user` 的 `has_customers` 检查保持不变；已删除客户计入。
- 所有写用例自行 `commit`。

- [ ] **Step 5: 修改路由**

按 Interfaces 修改 `app/api/customers.py`。`transfer_customer_owner` 的依赖改为 `Depends(get_current_context)`（已有 `context` 参数，去掉 `require_permission` 装饰），权限由服务层判定。

- [ ] **Step 6: 修改旧的硬删除断言**

- `tests/test_customer_repository.py::test_get_by_id_and_delete_roundtrip`：改为调用 `soft_delete`，断言行仍存在且 `status == DELETED`。
- `tests/test_customer_e2e.py` 约 340 行：转移场景的 404 断言来自可见性（原设计 10.3），保持不变。

- [ ] **Step 7: 运行客户相关测试确认通过**

Run: `.venv/bin/pytest tests/test_customer_soft_delete.py tests/test_customer_repository.py tests/test_customer_service.py tests/test_customer_api.py tests/test_customer_e2e.py tests/test_customer_audit.py tests/test_user_crud.py -v`
Expected: 全部 PASS。

- [ ] **Step 8: 提交**

```bash
git add app/repositories/customer.py app/services/customer.py app/api/customers.py tests
git commit -m "feat: soft delete and restore customers"
```

---

### Task 4: 格式校验与仓储

**Files:**
- Create: `app/services/contact_validation.py`
- Create: `app/repositories/contact.py`
- Test: `tests/test_contact_validation.py`、`tests/test_contact_repository.py`

**Interfaces:**
- Produces（`app/services/contact_validation.py`）：
  - `normalize_phone(value: str | None) -> str | None`：`strip()` 后为空返回 `None`；去除内部空格与 `-`；允许首字符 `+`；其余必须为数字；数字位数 7–20；否则 `ValueError`。
  - `normalize_email(value: str | None) -> str | None`：`strip()` 后为空返回 `None`；长度 > 255 或不匹配 `^[^@\s]+@[^@\s]+\.[^@\s]+$` 抛 `ValueError`；返回 `strip()` 后的值。
- Produces（`app/repositories/contact.py`）：
  - `get_function(session, function_id) -> ContactFunction | None`（不过滤状态）。
  - `list_functions_for_customer(session, customer_id) -> list[ContactFunction]`：`customer_id IS NULL OR customer_id = :cid`，`status = ACTIVE`，按 `name`。
  - `list_shared_functions(session, *, status: ContactStatus) -> list[ContactFunction]`。
  - `lock_function_for_update(session, function_id) -> ContactFunction | None`：`with_for_update()`。
  - `lock_function_shared(session, function_id) -> ContactFunction | None`：`with_for_update(read=True)`。
  - `count_contacts_referencing(session, function_id) -> int`：不过滤对接人状态。
  - `reassign_contacts(session, from_function_id, to_function_id) -> int`：批量 UPDATE，返回 `rowcount`。
  - `list_contacts(session, customer_id, *, status, page, page_size) -> tuple[list[tuple[CustomerContact, ContactFunction]], int]`：JOIN 职能，排序 `ContactFunction.name, CustomerContact.name, CustomerContact.id`。
  - `get_contact(session, customer_id, contact_id) -> CustomerContact | None`。

- [ ] **Step 1: 写失败的校验测试**

```python
# tests/test_contact_validation.py
import pytest

from app.services.contact_validation import normalize_email, normalize_phone


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_phone_becomes_none(raw: str | None) -> None:
    assert normalize_phone(raw) is None


def test_phone_strips_spaces_and_hyphens() -> None:
    assert normalize_phone("+86 138-0013-8000") == "+8613800138000"


def test_plain_mobile_is_accepted() -> None:
    assert normalize_phone("13800138000") == "13800138000"


@pytest.mark.parametrize("raw", ["12345", "abc1234567", "1" * 21, "+", "1+2345678"])
def test_invalid_phone_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_email_becomes_none(raw: str | None) -> None:
    assert normalize_email(raw) is None


def test_email_is_stripped_and_accepted() -> None:
    assert normalize_email(" a@b.com ") == "a@b.com"


@pytest.mark.parametrize("raw", ["no-at-sign", "a@b", "a b@c.com", "x@" + "y" * 260 + ".com"])
def test_invalid_email_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(raw)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_validation.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现校验模块，运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_validation.py -v`
Expected: PASS

- [ ] **Step 4: 写失败的仓储测试**

```python
# tests/test_contact_repository.py
from app.models.contact import ContactStatus
from app.repositories import contact as contact_repository
from tests.contact_helpers import add_contact, add_function  # 同 customer_helpers 风格，Task 4 内新建


async def test_list_functions_for_customer_includes_shared_and_own_only(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    customer_b = await make_customer_row(session, owner=owner)
    await add_function(session, name="商务", customer_id=None)
    await add_function(session, name="渠道对接", customer_id=customer_a.id)
    await add_function(session, name="他客户的", customer_id=customer_b.id)
    await add_function(session, name="已删旧", customer_id=None, status=ContactStatus.DELETED)

    names = [f.name for f in await contact_repository.list_functions_for_customer(session, customer_a.id)]
    assert names == ["商务", "渠道对接"]


async def test_count_contacts_includes_deleted_contacts(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    fn = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, customer_a, fn, name="张三", status=ContactStatus.ACTIVE)
    await add_contact(session, customer_a, fn, name="李四", status=ContactStatus.DELETED)

    assert await contact_repository.count_contacts_referencing(session, fn.id) == 2


async def test_reassign_moves_all_contacts_including_deleted(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    src = await add_function(session, name="研发", customer_id=None)
    dst = await add_function(session, name="产品", customer_id=None)
    await add_contact(session, customer_a, src, name="张三", status=ContactStatus.ACTIVE)
    await add_contact(session, customer_a, src, name="李四", status=ContactStatus.DELETED)

    moved = await contact_repository.reassign_contacts(session, src.id, dst.id)
    assert moved == 2
    assert await contact_repository.count_contacts_referencing(session, src.id) == 0
    assert await contact_repository.count_contacts_referencing(session, dst.id) == 2


async def test_list_contacts_paginates_and_orders(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    fn_b = await add_function(session, name="运营", customer_id=None)
    fn_a = await add_function(session, name="商务", customer_id=None)
    for name in ["王五", "张三", "赵六"]:
        await add_contact(session, customer_a, fn_a, name=name)
    await add_contact(session, customer_a, fn_b, name="孙七")
    await add_contact(session, customer_a, fn_b, name="钱八")

    items, total = await contact_repository.list_contacts(
        session, customer_a.id, status=ContactStatus.ACTIVE, page=1, page_size=3
    )
    assert total == 5
    assert [c.name for c, _ in items] == ["张三", "王五", "赵六"]  # 商务组按姓名


async def test_list_contacts_excludes_deleted(session) -> None:
    owner = await make_user(session)
    customer_a = await make_customer_row(session, owner=owner)
    fn = await add_function(session, name="商务", customer_id=None)
    await add_contact(session, customer_a, fn, name="在岗", status=ContactStatus.ACTIVE)
    await add_contact(session, customer_a, fn, name="离岗", status=ContactStatus.DELETED)

    items, total = await contact_repository.list_contacts(
        session, customer_a.id, status=ContactStatus.ACTIVE, page=1, page_size=20
    )
    assert total == 1
    assert items[0][0].name == "在岗"
```

注：`tests/contact_helpers.py` 在本任务中创建并验证（签名见上方共享 helper 说明）。`customer_a`、`customer_b` 不是 fixture，每个测试体内先执行 `owner = await make_user(session)` 与 `customer_a = await make_customer_row(session, owner=owner)`（需要时再创建 `customer_b`）。`session` 来自 `tests.customer_helpers`。

- [ ] **Step 5: 运行确认失败，实现仓储，运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_repository.py -v`
Expected: FAIL → 实现 → PASS

- [ ] **Step 6: 提交**

```bash
git add app/services/contact_validation.py app/repositories/contact.py tests/test_contact_validation.py tests/test_contact_repository.py tests/contact_helpers.py
git commit -m "feat: add contact validation and repository"
```

---

### Task 5: 职能服务（共用、自定义、删除迁移、恢复）

**Files:**
- Create: `app/services/contact.py`
- Test: `tests/test_contact_function_service.py`

**Interfaces:**
- Consumes: Task 4 仓储；Task 3 的 `customer_repository`、可见性过滤（`build_customer_scope_filter`）。
- Produces（共用，仅超管）：
  - `list_shared_functions(session, *, context, include_deleted: bool = False) -> list[ContactFunction]`：`include_deleted=True` 且非超管 → 403。
  - `create_shared_function(session, name: str, *, context) -> ContactFunction`：`@audited(action="contact_function:create", resource_type="contact_function", resource_id_attr="id")`。
  - `update_shared_function(session, function_id: int, name: str, *, context) -> ContactFunction`：`@audited(action="contact_function:update", resource_type="contact_function", resource_id_param="function_id")`。
  - `delete_shared_function(session, function_id: int, *, replacement_function_id: int | None, context) -> int`：`@audited(action="contact_function:delete", resource_type="contact_function", resource_id_param="function_id")`，返回迁移人数。
  - `restore_shared_function(session, function_id: int, *, context) -> ContactFunction`：`@audited(action="contact_function:restore", resource_type="contact_function", resource_id_param="function_id")`。
- Produces（自定义）：
  - `list_customer_functions(session, customer_id: int, *, context) -> list[tuple[ContactFunction, Literal["SHARED","CUSTOM"]]]`：客户须可见且 ACTIVE（否则 `7001`）。
  - `create_custom_function(session, customer_id: int, name: str, *, context) -> ContactFunction`：`@audited(action="contact_function:create", resource_type="contact_function", resource_id_attr="id")`。
  - `update_custom_function(session, customer_id: int, function_id: int, name: str, *, context) -> ContactFunction`：`@audited(action="contact_function:update", resource_type="contact_function", resource_id_param="function_id")`。
  - `delete_custom_function(session, customer_id: int, function_id: int, *, replacement_function_id: int | None, context) -> int`：`@audited(action="contact_function:delete", resource_type="contact_function", resource_id_param="function_id")`。
  - `restore_custom_function(session, customer_id: int, function_id: int, *, context) -> ContactFunction`：`@audited(action="contact_function:restore", resource_type="contact_function", resource_id_param="function_id")`。
  - `list_customer_recycle_functions(session, customer_id: int, *, context) -> list[ContactFunction]`。
- 名称冲突规则（`_assert_name_available`）：
  - 共用职能新增或改名：与任一 ACTIVE 共用职能同名（排除自身）→ 7007；与任一 **ACTIVE 自定义职能**（任意客户）同名 → 7007。
  - 自定义职能新增或改名：与同客户 ACTIVE 自定义职能同名（排除自身）→ 7007；与 ACTIVE 共用职能同名 → 7007。
- 删除迁移规则（`_resolve_replacement`，被删职能为 `target`）：
  - 替代为 None：若 `n > 0` → 7008；`n == 0` 可通过。
  - 替代非 None：若 `n == 0` → 7009。
  - 替代不存在、非 ACTIVE、等于 `target.id` → 7009。
  - `target` 为共用：替代必须 `customer_id IS NULL`。
  - `target` 为自定义：替代必须 `customer_id IS NULL` 或 `customer_id == target.customer_id`。
  - 不合法 → 7009。
- 删除流程（`_delete_function`，两种删除共用）：
  1. `lock_function_for_update(function_id)`；不存在或非 ACTIVE → 7006。自定义删除额外校验 `target.customer_id == customer_id`，否则 7006。
  2. 若 `replacement_function_id` 非 None：`lock_function_shared(replacement_function_id)`，按上表校验。
  3. `n = count_contacts_referencing(function_id)`，按上表判定 7008 / 7009。
  4. `n > 0` 时 `reassign_contacts(function_id, replacement)`。
  5. `target.status = DELETED`，`deleted_at`、`deleted_by = context.user_id`。
  6. `commit()`，返回 `n`。
- 超管判定：共用职能写操作与回收站仅 `context.is_super_admin`；否则 `BusinessError(FORBIDDEN, 403)`。服务层判定，不依赖路由权限码。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contact_function_service.py
import pytest
from sqlalchemy import select

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactFunction, ContactStatus
from app.services.contact import (
    create_custom_function,
    create_shared_function,
    delete_custom_function,
    delete_shared_function,
    list_customer_functions,
    restore_shared_function,
    update_shared_function,
)
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401

SUPER = dict(is_super_admin=True)


async def test_non_super_admin_cannot_create_shared(session) -> None:
    user = await make_user(session)
    with pytest.raises(BusinessError) as exc:
        await create_shared_function(session, "商务", context=make_context(user, permission_codes={"customer:update"}))
    assert exc.value.http_status == 403


async def test_shared_name_clashing_with_active_custom_is_rejected(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="渠道对接", customer_id=owner.id)

    with pytest.raises(BusinessError) as exc:
        await create_shared_function(session, "渠道对接", context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_custom_name_clashing_with_shared_is_rejected(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="商务", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await create_custom_function(session, owner.id, "商务", context=make_context(user, permission_codes={"customer:update"}))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_delete_without_contacts_needs_no_replacement(session) -> None:
    user = await make_user(session)
    fn = await add_function(session, name="临时", customer_id=None)

    moved = await delete_shared_function(session, fn.id, replacement_function_id=None, context=make_context(user, **SUPER))
    assert moved == 0
    row = await session.scalar(select(ContactFunction).where(ContactFunction.id == fn.id))
    assert row.status == ContactStatus.DELETED


async def test_delete_with_contacts_requires_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    fn = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, owner, fn, name="张三")

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(session, fn.id, replacement_function_id=None, context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_REQUIRED
    row = await session.scalar(select(ContactFunction).where(ContactFunction.id == fn.id))
    assert row.status == ContactStatus.ACTIVE


async def test_delete_without_contacts_rejects_replacement(session) -> None:
    user = await make_user(session)
    fn = await add_function(session, name="研发", customer_id=None)
    other = await add_function(session, name="产品", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(session, fn.id, replacement_function_id=other.id, context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_shared_delete_rejects_custom_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    fn = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, owner, fn, name="张三")
    custom = await add_function(session, name="渠道", customer_id=owner.id)

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(session, fn.id, replacement_function_id=custom.id, context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_shared_delete_rejects_deleted_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    fn = await add_function(session, name="研发", customer_id=None)
    await add_contact(session, owner, fn, name="张三")
    gone = await add_function(session, name="旧", customer_id=None, status=ContactStatus.DELETED)

    with pytest.raises(BusinessError) as exc:
        await delete_shared_function(session, fn.id, replacement_function_id=gone.id, context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_custom_delete_allows_own_or_shared_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:update", "customer:delete"})
    own = await add_function(session, name="渠道", customer_id=owner.id)
    shared = await add_function(session, name="商务", customer_id=None)
    own_other = await add_function(session, name="渠道二", customer_id=owner.id)
    await add_contact(session, owner, own, name="A")
    await add_contact(session, owner, own_other, name="B")

    assert await delete_custom_function(session, owner.id, own.id, replacement_function_id=shared.id, context=ctx) == 1


async def test_custom_delete_rejects_other_customer_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    other_owner = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:update", "customer:delete"})
    own = await add_function(session, name="渠道", customer_id=owner.id)
    foreign = await add_function(session, name="外部", customer_id=other_owner.id)
    await add_contact(session, owner, own, name="A")

    with pytest.raises(BusinessError) as exc:
        await delete_custom_function(session, owner.id, own.id, replacement_function_id=foreign.id, context=ctx)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_delete_rejects_self_as_replacement(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:update", "customer:delete"})
    own = await add_function(session, name="渠道", customer_id=owner.id)
    await add_contact(session, owner, own, name="A")

    with pytest.raises(BusinessError) as exc:
        await delete_custom_function(session, owner.id, own.id, replacement_function_id=own.id, context=ctx)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID


async def test_delete_migrates_soft_deleted_contacts_too(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    src = await add_function(session, name="研发", customer_id=None)
    dst = await add_function(session, name="产品", customer_id=None)
    gone = await add_contact(session, owner, src, name="离岗", status=ContactStatus.DELETED)

    await delete_shared_function(session, src.id, replacement_function_id=dst.id, context=make_context(user, **SUPER))
    await session.refresh(gone)
    assert gone.function_id == dst.id


async def test_restore_does_not_move_contacts_back(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    src = await add_function(session, name="研发", customer_id=None)
    dst = await add_function(session, name="产品", customer_id=None)
    contact = await add_contact(session, owner, src, name="张三")
    await delete_shared_function(session, src.id, replacement_function_id=dst.id, context=make_context(user, **SUPER))

    await restore_shared_function(session, src.id, context=make_context(user, **SUPER))
    await session.refresh(contact)
    assert contact.function_id == dst.id


async def test_restore_shared_name_conflict_returns_7007(session) -> None:
    user = await make_user(session)
    gone = await add_function(session, name="研发", customer_id=None, status=ContactStatus.DELETED)
    await add_function(session, name="研发", customer_id=None)  # 同名 ACTIVE

    with pytest.raises(BusinessError) as exc:
        await restore_shared_function(session, gone.id, context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NAME_EXISTS


async def test_restore_active_function_returns_7012(session) -> None:
    user = await make_user(session)
    fn = await add_function(session, name="研发", customer_id=None)

    with pytest.raises(BusinessError) as exc:
        await restore_shared_function(session, fn.id, context=make_context(user, **SUPER))
    assert exc.value.code == ErrorCode.RECORD_NOT_DELETED


async def test_functions_of_deleted_customer_are_not_visible(session) -> None:
    user = await make_user(session)
    owner = await make_customer_row(session, owner=user)
    await add_function(session, name="渠道", customer_id=owner.id)
    owner.status = ContactStatus.DELETED  # 直接置位以模拟已删除客户
    await session.flush()

    with pytest.raises(BusinessError) as exc:
        await list_customer_functions(session, owner.id, context=make_context(user, permission_codes={"customer:read"}))
    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_custom_function_belonging_to_other_customer_returns_7006(session) -> None:
    user = await make_user(session)
    a = await make_customer_row(session, owner=user)
    b = await make_customer_row(session, owner=user)
    foreign = await add_function(session, name="外部", customer_id=b.id)
    ctx = make_context(user, permission_codes={"customer:delete", "customer:update"})

    with pytest.raises(BusinessError) as exc:
        await delete_custom_function(session, a.id, foreign.id, replacement_function_id=None, context=ctx)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_NOT_FOUND
```

注：`add_function` 的 `status` 参数由 Task 4 的 `tests/contact_helpers.py` 提供。最后一个测试的名称与断言以 `CONTACT_FUNCTION_NOT_FOUND`（7006）为准，函数名应为 `..._returns_7006`，执行者需同步修正。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_function_service.py -v`
Expected: FAIL（服务模块不存在）

- [ ] **Step 3: 实现服务**

按 Interfaces 与删除流程实现。`@audited` 的 `resource_id_param="function_id"` 依赖参数名一致，三个删除/更新函数的签名必须使用 `function_id`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_function_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/services/contact.py tests/test_contact_function_service.py
git commit -m "feat: add contact function service with replacement migration"
```

---

### Task 6: 对接人服务

**Files:**
- Modify: `app/services/contact.py`（追加对接人函数）
- Test: `tests/test_contact_service.py`

**Interfaces:**
- Consumes: Task 4 的 `contact_validation`、仓储；Task 5 的职能查询。
- Produces：
  - `list_contacts(session, customer_id, *, context, page, page_size) -> tuple[list[tuple[CustomerContact, ContactFunction]], int]`：客户须可见且 ACTIVE（否则 `7001`）。`status=ACTIVE`。
  - `create_contact(session, customer_id, payload: ContactCreate, *, context) -> CustomerContact`：`@audited(action="contact:create", resource_type="customer_contact", resource_id_attr="id")`。
  - `update_contact(session, customer_id, contact_id, payload: ContactUpdate, *, context) -> CustomerContact`：`@audited(action="contact:update", resource_type="customer_contact", resource_id_param="contact_id")`。
  - `delete_contact(session, customer_id, contact_id, *, context) -> None`：`@audited(action="contact:delete", resource_type="customer_contact", resource_id_param="contact_id")`。软删除。
  - `restore_contact(session, customer_id, contact_id, *, context) -> CustomerContact`：`@audited(action="contact:restore", ...)`。非 DELETED → `7012`。
  - 对接人不存在、已删除或不属于该客户 → `7005`。
- 校验（`_validate_contact_fields`）：`name` 去空白后为空或超 255 → `7003`；`position` 超 128 → `7003`；`home_address` 超 512 → `7003`；`phone`、`email` 经 `contact_validation`，`ValueError` → `7003`。空白可选字段统一存为 `None`。
- 职能合法性（`_assert_function_usable(session, customer_id, function_id)`）：`get_function` 为空或非 ACTIVE → `7010`；`customer_id IS NOT NULL` 且 `!= customer_id` → `7010`。通过后对该职能 `lock_function_shared`，与职能删除串行化（Task 7 验证）。
- 字段类型：`ContactCreate` 与 `ContactUpdate` 的 schema 在 Task 8 的 `app/schemas/contact.py` 中定义；本任务先在 `app/schemas/contact.py` 创建这两个类（仅字段，不含路由），以便服务层导入。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contact_service.py
import pytest

from app.core.errors import BusinessError, ErrorCode
from app.models.contact import ContactStatus
from app.schemas.contact import ContactCreate, ContactUpdate
from app.services.contact import (
    create_contact,
    delete_contact,
    list_contacts,
    restore_contact,
    update_contact,
)
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401

PERMS = {"customer:read", "customer:update", "customer:delete"}


async def _setup(session):
    user = await make_user(session)
    ctx = make_context(user, permission_codes=PERMS)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    return user, ctx, customer, shared


async def test_create_with_shared_function_succeeds(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    created = await create_contact(
        session, customer.id, ContactCreate(name="张三", function_id=shared.id), context=ctx
    )
    assert created.id is not None
    assert created.function_id == shared.id
    assert created.status == ContactStatus.ACTIVE


async def test_create_with_other_customer_function_returns_7010(session) -> None:
    user, ctx, customer, _ = await _setup(session)
    other = await make_customer_row(session, owner=user)
    foreign = await add_function(session, name="外部", customer_id=other.id)

    with pytest.raises(BusinessError) as exc:
        await create_contact(session, customer.id, ContactCreate(name="张三", function_id=foreign.id), context=ctx)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_UNAVAILABLE


async def test_create_with_deleted_function_returns_7010(session) -> None:
    user, ctx, customer, _ = await _setup(session)
    gone = await add_function(session, name="旧", customer_id=None, status=ContactStatus.DELETED)

    with pytest.raises(BusinessError) as exc:
        await create_contact(session, customer.id, ContactCreate(name="张三", function_id=gone.id), context=ctx)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_UNAVAILABLE


async def test_invalid_phone_returns_7003(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    with pytest.raises(BusinessError) as exc:
        await create_contact(session, customer.id, ContactCreate(name="张三", function_id=shared.id, phone="12345"), context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_invalid_email_returns_7003(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    with pytest.raises(BusinessError) as exc:
        await create_contact(session, customer.id, ContactCreate(name="张三", function_id=shared.id, email="no-at"), context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_blank_name_returns_7003(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    with pytest.raises(BusinessError) as exc:
        await create_contact(session, customer.id, ContactCreate(name="   ", function_id=shared.id), context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD


async def test_blank_phone_and_email_stored_as_none(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    created = await create_contact(
        session, customer.id, ContactCreate(name="张三", function_id=shared.id, phone="  ", email=""), context=ctx
    )
    assert created.phone is None
    assert created.email is None


async def test_update_changes_function_to_shared(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    other_shared = await add_function(session, name="运营", customer_id=None)
    contact = await add_contact(session, customer, shared, name="张三")

    updated = await update_contact(session, customer.id, contact.id, ContactUpdate(function_id=other_shared.id), context=ctx)
    assert updated.function_id == other_shared.id


async def test_update_to_foreign_function_returns_7010(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    other = await make_customer_row(session, owner=user)
    foreign = await add_function(session, name="外部", customer_id=other.id)
    contact = await add_contact(session, customer, shared, name="张三")

    with pytest.raises(BusinessError) as exc:
        await update_contact(session, customer.id, contact.id, ContactUpdate(function_id=foreign.id), context=ctx)
    assert exc.value.code == ErrorCode.CONTACT_FUNCTION_UNAVAILABLE


async def test_contact_of_other_customer_returns_7005(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    other = await make_customer_row(session, owner=user)
    contact = await add_contact(session, other, shared, name="别人的")

    with pytest.raises(BusinessError) as exc:
        await update_contact(session, customer.id, contact.id, ContactUpdate(name="改"), context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_delete_contact_is_soft_and_hidden_from_list(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三")

    await delete_contact(session, customer.id, contact.id, context=ctx)
    await session.refresh(contact)
    assert contact.status == ContactStatus.DELETED

    _, total = await list_contacts(session, customer.id, context=ctx, page=1, page_size=20)
    assert total == 0


async def test_deleted_contact_update_returns_7005(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三", status=ContactStatus.DELETED)

    with pytest.raises(BusinessError) as exc:
        await update_contact(session, customer.id, contact.id, ContactUpdate(name="改"), context=ctx)
    assert exc.value.code == ErrorCode.CUSTOMER_CONTACT_NOT_FOUND


async def test_restore_contact_reactivates(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三")
    await delete_contact(session, customer.id, contact.id, context=ctx)

    restored = await restore_contact(session, customer.id, contact.id, context=ctx)
    assert restored.status == ContactStatus.ACTIVE


async def test_restore_active_contact_returns_7012(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    contact = await add_contact(session, customer, shared, name="张三")

    with pytest.raises(BusinessError) as exc:
        await restore_contact(session, customer.id, contact.id, context=ctx)
    assert exc.value.code == ErrorCode.RECORD_NOT_DELETED


async def test_list_hidden_customer_returns_7001(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    outsider = await make_user(session)
    outsider_ctx = make_context(outsider, permission_codes=PERMS, data_scope=DataScope.SELF)  # noqa: F821

    with pytest.raises(BusinessError) as exc:
        await list_contacts(session, customer.id, context=outsider_ctx, page=1, page_size=20)
    assert exc.value.code == ErrorCode.CUSTOMER_NOT_FOUND


async def test_list_is_paginated(session) -> None:
    user, ctx, customer, shared = await _setup(session)
    for i in range(5):
        await add_contact(session, customer, shared, name=f"人{i}")

    items, total = await list_contacts(session, customer.id, context=ctx, page=2, page_size=2)
    assert total == 5
    assert len(items) == 2
```

注：`DataScope` 需在测试文件顶部 `from app.models.role import DataScope` 导入（执行者添加，上面 `# noqa: F821` 处为占位标记，实现时删除）。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_service.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `app/schemas/contact.py` 创建 `ContactCreate`、`ContactUpdate`（字段见 Interfaces，`extra="forbid"`，name 最长 255 等 Schema 层长度约束）。在 `app/services/contact.py` 实现对接人函数。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/schemas/contact.py app/services/contact.py tests/test_contact_service.py
git commit -m "feat: add customer contact service"
```

---

### Task 7: 并发测试（职能删除与对接人写入）

**Files:**
- Test: `tests/test_contact_concurrency.py`

**Interfaces:**
- Consumes: Task 5 `delete_shared_function`，Task 6 `create_contact`。

该测试需要两个独立连接，不能复用 `session` fixture 的单事务，改用 `async_sessionmaker` 并在结束后清理。

- [ ] **Step 1: 写测试**

```python
# tests/test_contact_concurrency.py
import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog
from app.models.role import DataScope
from app.models.contact import ContactFunction, ContactStatus, CustomerContact
from app.models.customer import Customer
from app.models.department import Department  # noqa: F401  (外键依赖，清理顺序用)
from app.models.user import User
from app.schemas.contact import ContactCreate
from app.services.contact import create_contact, delete_shared_function
from tests.contact_helpers import add_function
from tests.customer_helpers import TEST_DATABASE_URL, make_context, make_customer_row, make_user


async def test_concurrent_create_and_function_delete_leave_no_dangling_reference() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # 1. 真实提交的准备数据（本测试不能用回滚型 savepoint fixture，因为两个连接需要看到彼此的提交）。
    async with factory() as setup:
        user = await make_user(setup)
        customer = await make_customer_row(setup, owner=user)
        f = await add_function(setup, name=f"并发目标-{user.id}", customer_id=None)
        g = await add_function(setup, name=f"并发替代-{user.id}", customer_id=None)
        await setup.commit()
        user_id, customer_id, f_id, g_id = user.id, customer.id, f.id, g.id

    # user 对象在 setup 会话结束后已分离，故不再使用 ORM 对象构造上下文，只保存其 ID。
    ctx = AuthContext(
        user_id=user_id,
        role_id=1,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset({"customer:update", "customer:delete"}),
        is_super_admin=True,
    )

    async def create() -> str:
        async with factory() as s:
            try:
                await create_contact(
                    s,
                    customer_id,
                    ContactCreate(name="并发", function_id=f_id),
                    context=ctx,
                )
                return "ok"
            except BusinessError as error:
                return f"err:{error.code}"

    async def delete() -> str:
        async with factory() as s:
            try:
                await delete_shared_function(
                    s,
                    f_id,
                    replacement_function_id=g_id,
                    context=ctx,
                )
                return "ok"
            except BusinessError as error:
                return f"err:{error.code}"

    try:
        create_result, delete_result = await asyncio.gather(create(), delete())

        # 2. 不变式：不存在指向已删除职能的对接人（无论谁先完成）。
        async with factory() as check:
            dangling = (
                await check.scalars(
                    select(CustomerContact)
                    .join(ContactFunction, CustomerContact.function_id == ContactFunction.id)
                    .where(
                        ContactFunction.status == ContactStatus.DELETED,
                        ContactFunction.id == f_id,
                    )
                )
            ).all()
        assert dangling == []

        # 3. 允许的结果组合：delete 必须成功；create 成功（随后被迁移）或因职能已删除返回 7010。
        assert delete_result == "ok"
        assert create_result in ("ok", f"err:{ErrorCode.CONTACT_FUNCTION_UNAVAILABLE}")
    finally:
        # 4. 清理：按外键依赖逆序删除，审计行一并清除。
        async with factory() as cleanup:
            await cleanup.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await cleanup.execute(delete(CustomerContact).where(CustomerContact.customer_id == customer_id))
            await cleanup.execute(delete(ContactFunction).where(ContactFunction.id.in_([f_id, g_id])))
            await cleanup.execute(delete(Customer).where(Customer.id == customer_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
        await engine.dispose()


- [ ] **Step 2: 运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_concurrency.py -v`
Expected: PASS

- [ ] **Step 3: 验证测试能捕获竞争（必须执行）**

临时注释掉 `app/services/contact.py` 中 `create_contact` 对职能的 `lock_function_shared` 调用，运行：
`.venv/bin/pytest tests/test_contact_concurrency.py -v --count=20`（若未安装 pytest-repeat，则用 `for i in $(seq 20); do .venv/bin/pytest tests/test_contact_concurrency.py -q || break; done`）。
Expected: 至少一次失败（出现悬挂引用）。记录结果后恢复注释。若从未失败，说明测试无效，需加大竞争窗口（例如在删除迁移与标记之间插入 `await asyncio.sleep(0)`，仅测试环境）后重试。

- [ ] **Step 4: 提交**

```bash
git add tests/test_contact_concurrency.py
git commit -m "test: cover concurrent contact create and function delete"
```

---

### Task 8: 接口层

**Files:**
- Modify: `app/schemas/contact.py`（追加响应与函数请求模型）
- Create: `app/api/contacts.py`
- Modify: `app/api/router.py`
- Test: `tests/test_contact_api.py`

**Interfaces:**
- Consumes: Task 5、6 的服务；`require_permission`；`get_session`；`get_current_context`；`app.core.response` 的 `Page`、`MAX_PAGE_SIZE`、`success`。
- Produces（`app/schemas/contact.py`）：
  - `ContactFunctionCreate(name: str)`、`ContactFunctionUpdate(name: str)`：`extra="forbid"`，`name` 长度 1–64。
  - `ContactFunctionRead(id, name, customer_id, scope, created_at, updated_at)`。
  - `ContactRead(id, customer_id, function_id, function_name, function_scope, name, position, phone, email, home_address, remark, created_at, updated_at)`。
- 路由（`app/api/contacts.py`，`APIRouter`，前缀由各路由显式写出）：
  - `GET /contact-functions`：`customer:read`；服务 `list_shared_functions(include_deleted=False)`。
  - `POST /contact-functions`：`get_current_context`；服务 `create_shared_function`（超管判定在服务层）。
  - `PATCH /contact-functions/{function_id}`：`get_current_context`；`update_shared_function`。
  - `DELETE /contact-functions/{function_id}?replacement_function_id=`：`get_current_context`；`delete_shared_function`。
  - `GET /contact-functions/recycle-bin`：`get_current_context`；`list_shared_functions(include_deleted=True)`。**声明在 `/{function_id}` 之前**。
  - `POST /contact-functions/{function_id}/restore`：`get_current_context`；`restore_shared_function`。
  - `GET /customers/{customer_id}/contact-functions`：`customer:read`；`list_customer_functions`。
  - `POST /customers/{customer_id}/contact-functions`：`customer:update`；`create_custom_function`。
  - `PATCH /customers/{customer_id}/contact-functions/{function_id}`：`customer:update`；`update_custom_function`。
  - `DELETE /customers/{customer_id}/contact-functions/{function_id}?replacement_function_id=`：`customer:delete`；`delete_custom_function`。
  - `GET /customers/{customer_id}/contact-functions/recycle-bin`：`customer:delete`；`list_customer_recycle_functions`。**声明在 `/{function_id}` 之前**。
  - `POST /customers/{customer_id}/contact-functions/{function_id}/restore`：`customer:delete`；`restore_custom_function`。
  - `GET /customers/{customer_id}/contacts?page=&page_size=`：`customer:read`；`Page[ContactRead]`。
  - `POST /customers/{customer_id}/contacts`：`customer:update`。
  - `PATCH /customers/{customer_id}/contacts/{contact_id}`：`customer:update`。
  - `DELETE /customers/{customer_id}/contacts/{contact_id}`：`customer:delete`。
  - `GET /customers/{customer_id}/contacts/recycle-bin?page=&page_size=`：`customer:delete`。**声明在 `/{contact_id}` 之前**。
  - `POST /customers/{customer_id}/contacts/{contact_id}/restore`：`customer:delete`。
- 审计：每个写路由调用 `register_audit_route(method, path, action=..., resource_type=...)`，路径模板使用 `/customers/{customer_id}/...` 形式（不含 `/api/v1`），与 `app/api/customers.py` 的注册约定一致（已确认 `exception_handlers.py:141` 使用同样的 route path）。
- 超管接口的 `get_current_context` 依赖不做权限码检查；`is_super_admin` 判定在服务层。非超管调用共用职能写接口由服务层返回 403。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contact_api.py
import pytest

from app.core.audit_meta import get_audit_route_meta
from tests.customer_helpers import api_client, make_context, make_customer_row, make_user, session  # noqa: F401
from tests.contact_helpers import add_contact, add_function


async def test_shared_function_write_requires_super_admin(session) -> None:
    user = await make_user(session)
    ctx = make_context(user, permission_codes={"customer:update", "customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.post("/api/v1/contact-functions", json={"name": "商务"})
    assert response.status_code == 403


async def test_super_admin_creates_shared_function(session) -> None:
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.post("/api/v1/contact-functions", json={"name": "商务"})
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "商务"


async def test_custom_function_requires_customer_update(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.post(f"/api/v1/customers/{customer.id}/contact-functions", json={"name": "渠道"})
    assert response.status_code == 403


async def test_contact_delete_requires_customer_delete_not_update(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, shared, name="张三")
    ctx = make_context(user, permission_codes={"customer:read", "customer:update"})
    async with api_client(session, ctx) as client:
        response = await client.delete(f"/api/v1/customers/{customer.id}/contacts/{contact.id}")
    assert response.status_code == 403


async def test_contacts_page_size_above_limit_is_rejected(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.get(f"/api/v1/customers/{customer.id}/contacts", params={"page_size": 101})
    assert response.status_code == 422


async def test_contacts_page_beyond_range_returns_empty_not_500(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.get(f"/api/v1/customers/{customer.id}/contacts", params={"page": 99999})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == []
    assert body["total"] == 0


async def test_recycle_bin_route_not_shadowed_by_contact_id(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read", "customer:delete"})
    async with api_client(session, ctx) as client:
        response = await client.get(f"/api/v1/customers/{customer.id}/contacts/recycle-bin")
    assert response.status_code == 200


async def test_error_codes_surface_over_http(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    await add_contact(session, customer, shared, name="张三")
    ctx = make_context(user, is_super_admin=True, permission_codes={"customer:update", "customer:delete"})
    async with api_client(session, ctx) as client:
        response = await client.delete(f"/api/v1/contact-functions/{shared.id}")
    assert response.status_code == 422
    assert response.json()["code"] == 7008


@pytest.mark.parametrize(
    ("method", "template", "action", "resource_type"),
    [
        ("POST", "/customers/{customer_id}/contacts", "contact:create", "customer_contact"),
        ("PATCH", "/customers/{customer_id}/contacts/{contact_id}", "contact:update", "customer_contact"),
        ("DELETE", "/customers/{customer_id}/contacts/{contact_id}", "contact:delete", "customer_contact"),
        ("POST", "/customers/{customer_id}/contacts/{contact_id}/restore", "contact:restore", "customer_contact"),
        ("POST", "/contact-functions", "contact_function:create", "contact_function"),
        ("PATCH", "/contact-functions/{function_id}", "contact_function:update", "contact_function"),
        ("DELETE", "/contact-functions/{function_id}", "contact_function:delete", "contact_function"),
        ("POST", "/contact-functions/{function_id}/restore", "contact_function:restore", "contact_function"),
        ("POST", "/customers/{customer_id}/restore", "customer:restore", "customer"),
    ],
)
def test_write_routes_are_registered_for_audit(method, template, action, resource_type) -> None:
    meta = get_audit_route_meta(method, template)
    assert meta is not None
    assert meta.action == action
    assert meta.resource_type == resource_type
```

注：`api_client(session, ctx)` 在 `tests/test_customer_api.py` 中定义，需抽到 `tests/customer_helpers.py`（Task 3 步骤中一并完成，或本任务开始时完成）。`/customers/{customer_id}/restore` 的客户恢复路由已在 Task 3 注册，本任务只校验注册存在。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_api.py -v`
Expected: FAIL（路由不存在）

- [ ] **Step 3: 实现**

- `app/api/contacts.py` 风格与 `app/api/customers.py` 一致。路由函数中 `session: Annotated[AsyncSession, Depends(get_session)]`，`context: Annotated[AuthContext, Depends(get_current_context)]`。
- 查询参数 `replacement_function_id: Annotated[int | None, Query()] = None`。
- 分页参数：`page: Annotated[int, Query(ge=1)] = 1`，`page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20`。
- 在 `app/api/router.py` 中 `api_v1_router.include_router(contacts_router)`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_contact_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/schemas/contact.py app/api/contacts.py app/api/router.py tests/test_contact_api.py tests/customer_helpers.py
git commit -m "feat: add contact and contact function APIs"
```

---

### Task 9: 共用职能种子数据

**Files:**
- Modify: `scripts/init_data.py`
- Test: `tests/test_contact_seed.py`

**Interfaces:**
- Produces: `async def _ensure_shared_contact_functions(session: AsyncSession) -> None`。按名称幂等写入 `商务、运营、研发、产品`，`customer_id=None`，`scope_key=0`，`status=ACTIVE`。已存在（`customer_id IS NULL AND active_name = ?`）则跳过。在 `init_data` 中于 `session.commit()` 之前调用。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contact_seed.py
from sqlalchemy import select

from app.models.contact import ContactFunction
from scripts.init_data import _ensure_shared_contact_functions
from tests.customer_helpers import session  # noqa: F401


async def test_shared_functions_are_seeded_once(session) -> None:
    await _ensure_shared_contact_functions(session)
    await _ensure_shared_contact_functions(session)

    rows = (
        await session.scalars(select(ContactFunction).where(ContactFunction.customer_id.is_(None)))
    ).all()
    assert sorted(r.name for r in rows) == ["产品", "商务", "研发", "运营"]
    assert all(r.scope_key == 0 for r in rows)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_contact_seed.py -v`
Expected: FAIL

- [ ] **Step 3: 实现并接入 `init_data`**

- [ ] **Step 4: 运行确认通过，并回归种子测试**

Run: `.venv/bin/pytest tests/test_contact_seed.py tests/test_seed_data.py -v`
Expected: PASS。`EXPECTED_PERMISSION_CODES` 未变，`test_seed_data.py` 应保持绿色。

- [ ] **Step 5: 提交**

```bash
git add scripts/init_data.py tests/test_contact_seed.py
git commit -m "feat: seed shared contact functions"
```

---

### Task 10: 全量回归与代码质量

**Files:** 无新增。

- [ ] **Step 1: 运行全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全部通过。基线为 113 项客户相关测试通过，全量基线需在 Task 1 开始前记录一次。失败则回到对应任务修复，不得跳过。

- [ ] **Step 2: 运行 lint 与类型检查**

Run: `.venv/bin/ruff check app tests scripts migrations && .venv/bin/ruff format --check app tests scripts migrations && .venv/bin/mypy app`
Expected: 无错误。

- [ ] **Step 3: 确认 alembic head 唯一**

Run: `JWT_SECRET=x CONFIG_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") .venv/bin/alembic heads`
Expected: 仅一个 head。

- [ ] **Step 4: 提交（如有格式修正）**

```bash
git add -u
git commit -m "chore: format and lint contact feature"
```

---

## Self-Review 记录

- **Spec 覆盖**：6.1 → Task 2、5；6.2 → Task 2、6；6.3 → Task 3、8；6.4 → Task 9；6.5 → Task 2；6.6 → Task 4；7 → Task 3、8；8.1–8.2 → Task 6；8.3 → Task 5、7；8.4 → Task 3；8.5 → Task 3；8.6 → Task 3、5、6；8.7 → Task 3；8.8 → Task 3；9 → Task 1；11 → Task 3、5、6、8；12 → 各任务。
- **已修正（自检发现）**：
  - 转移路由原依赖 `customer:update`，会挡住只有 `customer:delete` 的已删除客户转移（与 spec 8.5 不一致）。已改为服务层按状态判定。
  - 删除客户的 `delete_user` 集成测试需要超管上下文；`make_context` 需支持 `permission_codes` 参数。
  - Task 5 末尾测试名误写为 `returns_6006`，已在注中标注需改为 `7006`。
- **类型一致性**：`ContactStatus` 在 Task 1 定义，Task 2 起统一使用；`soft_delete` 替代旧 `delete`，调用点在 Task 3 同步；`lock_function_for_update`、`lock_function_shared`、`reassign_contacts`、`count_contacts_referencing` 在 Task 4 定义，Task 5–7 使用。
- **未完全消除的依赖**：`tests/contact_helpers.py` 与 `tests/customer_helpers.py` 的 fixture 签名在 Task 3、4、8 中由执行者统一抽取，计划在对应步骤中写明。
- **风险**：Task 7 的并发测试是否能稳定复现竞争窗口，需按 Step 3 实测验证；若不能，锁的正确性只能靠代码审查保证。
