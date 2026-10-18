# Employee Self-Profile Space and Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every active employee maintain their own profile, hide any business-card field from colleagues, and browse a company directory that every logged-in employee can open.

**Architecture:** Add a one-to-one `user_profile` table for business-card and private fields; `nickname`/`avatar` stay on `user`. A single normalization entry point (`profile_values`) plus a single clipping module (`app/services/directory.py`) turn one stored record into two response views — the edit view never clips, the colleague view always does. The directory is one `user` LEFT JOIN `user_profile` LEFT JOIN `department` paginated query: no data-scope filter, no new permission codes.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, asyncmy/MySQL 8, Alembic, Redis, pytest

**Spec:** `docs/superpowers/specs/2026-10-18-user-profile-space-design.md`

## Global Constraints

- Layer strictly `api → services → repositories → models`. No cross-layer calls.
- Every response goes through `app.core.response.success()` and `SuccessEnvelope`.
- **No new `ErrorCode`.** Bad format/params → `ErrorCode.INVALID_PARAMETER` (1006, `http_status=422`); missing target → `ErrorCode.USER_NOT_FOUND` (2000, `http_status=404`); rate limit → `ErrorCode.RATE_LIMITED` (1005, `http_status=429`).
- **No new `permission_code`.** All eight endpoints require only a login (`Depends(get_current_context)`); five are under `/me/profile`, three under `/directory`.
- Length caps live in Pydantic `max_length` and return 422; **never truncate on write**, never lean on MySQL column width.
- `expertise_tags`, `personal_links`, `hidden_public_fields` **never return `null` and are never written as `NULL`** — always normalized to a Python `list` (missing row / `NULL` / empty array all serialize as `[]`).
- Every external identifier is `user.id`; `user_profile.id` never appears in any response.
- Enum-like model fields use `StrEnum` + SQLAlchemy `Enum(values_callable=...)` as the existing models do.
- Test DB `mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test`, Redis `redis://localhost:6379/15`. Alembic needs `JWT_SECRET` and `CONFIG_ENCRYPTION_KEY` in the environment (see `tests/conftest.py:9-15`).
- Alembic head is `619b22fb0301`. Exactly one new revision, `down_revision = "619b22fb0301"`, and `alembic current` must show a single head afterwards.
- `ruff check .` and `mypy .` must pass. `mypy` runs `strict = true` with the pydantic plugin, so every new function and model needs full annotations; `ruff` enforces line length 88 and import order (`I`).
- Conventional commits (`feat:` / `fix:` / `test:` / `refactor:`).
- Baseline known-failing: `tests/test_audit_denial_hooks.py` has 2 tests that fail only in a full-suite run due to cross-test ordering pollution. That file alone reports `7 passed`. It is pre-existing on HEAD — **do not fix it here, and do not relax any other assertion because of it.**

## Review Focus

These are the input classes the spec implies but whose tests someone may forget to write. Each line gets a test in the task that owns the code (noted in parentheses).

1. **A hidden field and an unfilled field must be indistinguishable to a colleague.** Both emit explicit `null`; no key pruning. (Task 8)
2. **`null` and `[]` are not interchangeable in the colleague view.** Hidden list fields become `null`; unhidden-but-empty list fields stay `[]`. A generic `if not value: return None` clipping helper breaks this. (Tasks 7, 8)
3. **A colleague whose own department was soft-deleted must still appear with a department name.** The directory must not filter `department.deleted_at` / `status` for display or sort. (Task 7)
4. **An employee with `nickname = NULL` must still be addressable.** `display_name` falls back to `username`, and sorting uses `COALESCE(nickname, username)`; a `None` must never produce `null` as the display name. (Task 7)
5. **Empty `PATCH {}` must be a no-op with no audit record.** The `audited` decorator is bound to the service function, so the empty-body branch has to live in the API layer. (Task 4)

---

### Task 1: `user_profile` Table, Model, and Repository

**Files:**
- Create: `app/models/user_profile.py`
- Create: `app/repositories/user_profile.py`
- Create: `app/services/user_profile.py` (only `profile_values` in this task; the rest arrives in Tasks 2–5)
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/<generated>_create_user_profile.py`
- Test: `tests/test_user_profile_model.py`

**Interfaces:**
- Consumes: `Base` (id/created_at/updated_at), `User` FK target, existing async-session patterns.
- Produces: `UserProfile`; `PUBLIC_PROFILE_FIELD_KEYS: tuple[str, ...]`; `get_by_user_id(session, user_id) -> UserProfile | None`; `lock_by_user_id(session, user_id) -> UserProfile | None`; `add(session, profile) -> UserProfile`; and in `app/services/user_profile.py`, `profile_values(profile: UserProfile | None) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_user_profile_model.py`, follow the `tests/test_system_setting_repository.py` fixture pattern (own engine, outer transaction, `AsyncSession(join_transaction_mode="create_savepoint")`, rollback at teardown). Assert:

- Inserting `UserProfile(user_id=<existing user>)` then `get_by_user_id` returns the row.
- A second insert with the same `user_id` raises `IntegrityError` and `error.orig.args[0] == 1062`.
- Inserting without setting the three JSON columns reads back as `[]`, never `None`.
- `profile_values(None)` returns every scalar as `None` and all three JSON keys as `[]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_user_profile_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.user_profile'`

- [ ] **Step 3: Implement the model**

Create `app/models/user_profile.py`. Declare `PUBLIC_PROFILE_FIELD_KEYS` in exactly this order (the order is the canonical sort order for `hidden_public_fields`, see Task 5):

```python
PUBLIC_PROFILE_FIELD_KEYS: tuple[str, ...] = (
    "signature",
    "bio",
    "work_phone",
    "extension",
    "work_email",
    "office_location",
    "seat",
    "expertise_tags",
    "personal_links",
    "avatar",
)
```

Columns on `UserProfile` (`__tablename__ = "user_profile"`, `user_id` is `BigInteger`, `ForeignKey("user.id", ondelete="RESTRICT")`, `nullable=False`, `unique=True`, `index=True`): `signature` `String(100)`, `bio` `Text`, `work_phone` `String(32)`, `extension` `String(16)`, `work_email` `String(255)`, `office_location` `String(100)`, `seat` `String(50)`, `expertise_tags` `JSON` (`nullable=False`, `default=list`), `personal_links` `JSON` (same), `hidden_public_fields` `JSON` (same), `personal_phone` `String(32)`, `birthday` `Date`, `emergency_contact_name` `String(50)`, `emergency_contact_phone` `String(32)`.

`birthday` must be `sa.Date`, not `mysql.DATETIME` — a `DATETIME` would accept a time component and make the "is it today?" comparison timezone-dependent. All three `JSON` columns are `nullable=False` with `default=list` so `NULL` can only arrive from a hand-written row.

- [ ] **Step 4: Register the model**

In `app/models/__init__.py` add `from app.models.user_profile import PUBLIC_PROFILE_FIELD_KEYS, UserProfile` and both names to `__all__`, keeping alphabetical order.

- [ ] **Step 5: Generate and hand-check the migration**

Run: `.venv/bin/alembic revision --autogenerate -m "create user profile"`

Then open the generated file and verify, editing by hand if needed:

- The only operation is `op.create_table("user_profile", ...)` carrying the unique constraint and index.
- `down_revision = "619b22fb0301"`.
- `downgrade()` contains only `op.drop_table("user_profile")`.
- **Delete any incidental changes autogenerate produced for other tables.** No backfill and no touching existing `user` rows.

- [ ] **Step 6: Verify upgrade and downgrade both run**

Run with `DATABASE_URL`, `JWT_SECRET`, and `CONFIG_ENCRYPTION_KEY` set:

```bash
alembic upgrade head && alembic downgrade -1 && alembic upgrade head && alembic current
```

Expected: three clean runs, and `alembic current` lists exactly one head.

- [ ] **Step 7: Implement the repository**

`app/repositories/user_profile.py` — the three signatures from the Interfaces block. `lock_by_user_id` uses `.with_for_update()` plus `.execution_options(populate_existing=True)` so a caller that already holds a stale instance sees the committed row. `add` does `session.add` + `await session.flush()` and returns the instance; it must not commit (the service owns the transaction boundary).

- [ ] **Step 8: Implement the normalization entry point**

In `app/services/user_profile.py` add `profile_values(profile: UserProfile | None) -> dict[str, Any]`. Returns a dict with all 14 profile fields. When `profile is None` every scalar is `None` and the three JSON keys are `[]`. When the row exists but a JSON column is `None`, that key is also `[]`.

This is the single place that decides "missing row" and "`NULL` column" mean the same thing. Every response model reads from it — do not add a second "if row exists" branch anywhere.

Note that this function returns the *raw* values, including whatever is in `hidden_public_fields`. It does **not** clip — clipping is the colleague view's job and lives only in `app/services/directory.py` (Task 7). Keeping normalize and clip as two separate stages is what lets Task 2's edit view ignore hiding while Task 8's colleague view applies it, using the same stored data.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_user_profile_model.py -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add app/models/user_profile.py app/models/__init__.py app/repositories/user_profile.py migrations/versions/ tests/test_user_profile_model.py app/services/user_profile.py
git commit -m "feat: add user_profile table, model, and repository"
```

---

### Task 2: `GET /api/v1/me/profile` — Edit View Read

**Files:**
- Create: `app/schemas/profile.py`
- Create: `app/api/profile.py`
- Modify: `app/services/user_profile.py`
- Modify: `app/api/router.py`
- Modify: `app/main.py`
- Modify: `tests/test_app_boot.py`
- Test: `tests/test_profile_api.py`

**Interfaces:**
- Consumes: `profile_values`, `get_by_user_id`, `user_repository.get_by_id`, `department_repository.get_by_id`.
- Produces: `ProfileRead` (with `PersonalLink`); `get_my_profile(session, *, user_id: int) -> ProfileRead`; `router` in `app/api/profile.py` with `prefix="/me/profile"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_profile_api.py`, copying the `api_client` skeleton from `tests/test_system_setting_api.py:113-144` with `dependency_overrides[get_current_context]` returning an `AuthContext` built with an **empty** `permission_codes` frozenset. Assert:

- `GET /api/v1/me/profile` returns 200 and `data` contains `id`, `username`, `account_email`, `department_id`, `department_name`, `user_status`, `feishu_open_id`, `nickname`, `avatar`, all business-card fields, all private fields, and `hidden_public_fields`.
- With no `user_profile` row: `expertise_tags == []`, `personal_links == []`, `hidden_public_fields == []`, `signature is None`.
- `"created_at" not in data` and `"id"` equals `user.id` (not `user_profile.id`).
- The edit view **ignores** hiding: set `hidden_public_fields=["signature", "bio"]` with both filled, then `GET /me/profile` still returns the real values. Reading your own profile must never be clipped.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_profile_api.py -q`
Expected: FAIL — 404 on `/api/v1/me/profile`

- [ ] **Step 3: Implement the schemas**

`app/schemas/profile.py` with `PersonalLink(BaseModel)` (`label: str`, `url: str`), `PersonalLinkInput(BaseModel)` (added in Task 3 — same two fields, used for request bodies), and `ProfileRead(BaseModel)` (`model_config = ConfigDict(from_attributes=True)`) carrying: `id: int`, `username: str | None`, `account_email: str | None`, `department_id: int | None`, `department_name: str | None`, `user_status: UserStatus`, `feishu_open_id: str | None`, `nickname: str | None`, `avatar: str | None`, the nine business-card fields (with `expertise_tags: list[str]`, `personal_links: list[PersonalLink]`), `hidden_public_fields: list[str]`, and the four private fields (`birthday: date | None`).

`PersonalLink` (response) and `PersonalLinkInput` (request) are two models rather than one because the request side needs normalization validators (label fallback, URL scheme check) that must not run when serializing stored data. Keep the output model free of validators.

`birthday` uses `@field_serializer("birthday", when_used="json")` to emit `YYYY-MM-DD`. There is deliberately **no** `created_at` — it is account-creation time, not hire date, and a frontend would render it as tenure.

- [ ] **Step 4: Implement the service**

`get_my_profile(session, *, user_id: int) -> ProfileRead` in `app/services/user_profile.py`: fetch the `User` (missing → `BusinessError(ErrorCode.USER_NOT_FOUND, "User not found", http_status=404)`), fetch the profile row, fetch `department_name` via `department_repository.get_by_id(session, user.department_id, include_deleted=True)` when `department_id` is not `None`, then build `ProfileRead` from `profile_values(profile)`.

`include_deleted=True` matters: the spec keeps the department name visible even after the department is soft-deleted, and the edit view must agree with the directory.

- [ ] **Step 5: Implement the endpoint and mount it**

`app/api/profile.py`: `router = APIRouter(prefix="/me/profile", tags=["个人资料"])`, `GET ""` with `response_model=SuccessEnvelope[ProfileRead]` and only `Depends(get_current_context)` — no `require_permission`.

In `app/api/router.py` import and `include_router` it, keeping the existing import ordering.

- [ ] **Step 6: Add the OpenAPI tag**

In `app/main.py` append `{"name": "个人资料", "description": "本人资料维护"}` to `OPENAPI_TAGS`. In `tests/test_app_boot.py::test_openapi_declares_business_domain_tags` add `"个人资料"` to the expected set (add `"通讯录"` in Task 7, not here).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_profile_api.py tests/test_app_boot.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/schemas/profile.py app/api/profile.py app/services/user_profile.py app/api/router.py app/main.py tests/test_app_boot.py tests/test_profile_api.py
git commit -m "feat: add read endpoint for the current user's profile"
```

---

### Task 3: `ProfileUpdate` Validation and Normalization

**Files:**
- Modify: `app/core/datetime_utils.py`
- Modify: `app/schemas/profile.py`
- Test: `tests/test_datetime_utils.py`
- Test: `tests/test_user_profile_service.py`

**Interfaces:**
- Consumes: `normalize_phone`, `normalize_email` from `app/services/contact_validation.py`.
- Produces: `BUSINESS_TIMEZONE: tzinfo`, `business_today() -> date`, `ProfileUpdate`, `PrivacyUpdate`, `PersonalLinkInput`, `public_link_host(url) -> str | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_datetime_utils.py`: with `datetime` monkeypatched so UTC is `2026-10-17 20:00`, `business_today()` returns `2026-10-18` (Beijing has already rolled over). Also assert `BUSINESS_TIMEZONE` resolves without raising even when `ZoneInfo` raises `ZoneInfoNotFoundError`.

`tests/test_user_profile_service.py` (schema-only assertions, no DB) — these are the spec's exact values:

- `ProfileUpdate()` is valid; `ProfileUpdate(avatar="x")` raises `ValidationError`.
- `extension`: `"8042"` valid, `"101"` valid (**3 digits** — this is the case `normalize_phone` would reject, so it is the one that proves the rule is independent), `"12a4"` invalid, `"1" * 16` valid, `"1" * 17` invalid. Also assert `normalize_phone("101")` raises, right next to these, so the test documents *why* the separate rule exists.
- `work_phone="138 0013 8000"` normalizes to `"13800138000"`; `work_phone="123"` invalid.
- `bio="x" * 2001` invalid, `"x" * 2000` valid.
- `expertise_tags=["Java", " Java ", ""]` normalizes to `["Java"]`; 11 tags invalid; a 21-character tag invalid.
- `personal_links=[{"label": "  ", "url": "https://a.example/x"}]` gives `label == "a.example"`; `url="javascript:alert(1)"` invalid; 4 links invalid.
- `birthday` = tomorrow (per `business_today()`) invalid; today valid.
- `nickname="   "` normalizes to `None`; 101 characters invalid.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_datetime_utils.py tests/test_user_profile_service.py -q`
Expected: FAIL — `ImportError` for `business_today`, `ProfileUpdate`

- [ ] **Step 3: Implement `business_today()`**

In `app/core/datetime_utils.py`:

```python
try:
    BUSINESS_TIMEZONE: tzinfo = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BUSINESS_TIMEZONE = timezone(timedelta(hours=8))


def business_today() -> date:
    return datetime.now(BUSINESS_TIMEZONE).date()
```

The `try/except` is required: `tzdata` is not a dependency, so Windows dev machines and slim containers can raise `ZoneInfoNotFoundError`. China has had no DST since 1991, so a fixed +08:00 is equivalent here. **Never use `date.today()`** — under UTC it would block Chinese users from picking today's birthday before 08:00. Add both names to `__all__`.

- [ ] **Step 4: Implement `ProfileUpdate` and `PrivacyUpdate`**

`ProfileUpdate` with `model_config = ConfigDict(extra="forbid")`: `nickname`, `signature`, `bio`, `work_phone`, `extension`, `work_email`, `office_location`, `seat`, `personal_phone`, `emergency_contact_name`, `emergency_contact_phone` as `str | None` with the spec's `max_length` caps; `birthday: date | None`; `expertise_tags: list[str] | None`; `personal_links: list[PersonalLinkInput] | None`. `PrivacyUpdate` with `extra="forbid"` and `hidden_fields: list[str]`.

**`avatar` is absent from `ProfileUpdate` on purpose** — `extra="forbid"` turns a submitted `avatar` into a 422, so an arbitrary URL can never bypass the byte sniffing, size cap, and rate limit of the upload endpoint.

**Do not put `Field(max_length=N)` on `expertise_tags` or `personal_links`.** Pydantic applies `max_length` to the **raw input list**, but the spec requires strip → drop-empty → dedupe **before** the count check. For `["Java", " Java ", ""]` the raw length is 3 while the normalized length is 1, so a `Field(max_length=...)` would reject valid input and accept invalid input. Count limits belong in the `mode="after"` validators, measured against the normalized list.

- [ ] **Step 5: Implement the validators**

- `trim_text` (`mode="before"`): `strip()` strings; empty string → `None`.
- `work_phone` / `personal_phone` / `emergency_contact_phone`: `normalize_phone`, `ValueError` → `ValueError("invalid phone number")`.
- `extension`: **do not reuse `normalize_phone`** (it requires 7–20 digits and would reject legitimate 3–6 digit extensions). Use `re.fullmatch(r"\d{1,16}", value)`.
- `work_email`: `normalize_email`.
- `expertise_tags` (`mode="after"`): strip each tag, drop empties, dedupe preserving first occurrence, **then** require `len(result) <= 10` and each item 1–20 characters. Pass `None` through unchanged.
- `personal_links` (`mode="after"`): for each item, if `label` is blank fall back to `urlsplit(url).hostname`; if that is also empty drop the item; **then** require `len(result) <= 3`. `url` must be absolute http/https, ≤ 500 characters, no whitespace/control characters, no backslashes, no credentials (mirror the reasoning of `_validate_http_url` in `app/schemas/system_setting.py` but implement it locally — do not import a private helper across modules). `label` is 1–30 characters **after** the fallback, so the fallback must run before the length check.
- `birthday`: greater than `business_today()` → `ValueError("birthday must not be in the future")`.
- `PrivacyUpdate.hidden_fields`: schema does not validate the whitelist; that happens in the service (Task 5) so the client sees a uniform `BusinessError`.

Add `public_link_host(url)` as a small module function so the label fallback is unit-testable without constructing a model.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_datetime_utils.py tests/test_user_profile_service.py -q`
Expected: PASS

- [ ] **Step 7: Lint and commit**

```bash
.venv/bin/ruff check app/schemas/profile.py app/core/datetime_utils.py
git add app/core/datetime_utils.py app/schemas/profile.py tests/test_datetime_utils.py tests/test_user_profile_service.py
git commit -m "feat: add profile update schema validation and business-day helper"
```

---

### Task 4: `PATCH /api/v1/me/profile` — Persist

**Files:**
- Modify: `app/services/user_profile.py`
- Modify: `app/api/profile.py`
- Test: `tests/test_user_profile_service.py`
- Test: `tests/test_profile_api.py`

**Interfaces:**
- Consumes: `ProfileUpdate`, `get_by_user_id`, `lock_by_user_id`, `add`, `get_my_profile`.
- Produces: `update_my_profile(session, payload, *, user_id: int, context: AuthContext) -> ProfileRead`, decorated with `@audited(action="user_profile:update", resource_type="user_profile", resource_id_param="user_id")`; `PATCH ""` route.

- [ ] **Step 1: Write the failing tests**

- `{"signature": "hi"}` changes `signature` and leaves `seat` as `None`.
- `{"signature": "a"}` then `{"signature": null}` restores `None`.
- `{}` returns 200 with the current profile, changes no row, and **creates no audit row**.
- `{"nickname": "新名字", "seat": "A1"}` updates both `user.nickname` and `user_profile.seat`.
- An invalid field (e.g. over-long `bio`) → 422, with **neither** `user.nickname` nor the profile changed (whole-request rollback).
- First `PATCH` with no existing row creates the row.
- `user.token_version` is unchanged across a nickname change.
- Concurrency: delete the row, then `PATCH` from two independent sessions via `asyncio.gather`; assert no 500 and exactly one row. If the race will not reproduce reliably, fall back to a unit test that raises `IntegrityError` from the insert path and asserts the savepoint rollback re-reads and writes.
- Audit: one success row with `action="user_profile:update"`, `resource_type="user_profile"`, `resource_id == user_id`, and no field values in any column.
- `avatar` cannot be written here: `{"avatar": "https://evil.example/x.png"}` → 422 and `user.avatar` unchanged. This is the guard that keeps an arbitrary remote URL from bypassing the byte sniffing, size cap, and rate limit of the upload endpoint.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_user_profile_service.py tests/test_profile_api.py -q`
Expected: FAIL — 405 on `PATCH`

- [ ] **Step 3: Implement the service**

`update_my_profile(...)` as declared in Interfaces:

1. `provided = payload.model_dump(exclude_unset=True)`.
2. Fetch the `User` (missing → `USER_NOT_FOUND`).
3. `nickname = provided.pop("nickname", _UNSET)`; when present set `user.nickname = nickname`. Do **not** bump `token_version` — `AuthContext` is rebuilt from the authoritative row on every request, so a rename is not a security change; wiring it to `_commit_user_security_change` would force a re-login for a nickname edit.
4. If no profile fields remain, skip the profile write.
5. `profile = await get_by_user_id(...)`; when `None`, insert inside `async with session.begin_nested():` and catch `IntegrityError`, checking `error.orig.args[0] == 1062` (the same test `app/services/user.py::_is_duplicate_username` uses) to detect the concurrent-first-write race. On a hit, re-read with `lock_by_user_id`. If it is still `None`, re-raise.
6. `setattr` each provided field. For `expertise_tags` / `personal_links`, write a `list` explicitly (`None` → `[]`).
7. `await session.commit()`, `await session.refresh(user)` when `nickname` changed, then return `await get_my_profile(...)`.

- [ ] **Step 4: Implement the endpoint with the empty-body branch**

In `app/api/profile.py`, `PATCH ""` with `response_model=SuccessEnvelope[ProfileRead]`. The empty-body branch goes **here, not in the service**:

```python
if not payload.model_dump(exclude_unset=True):
    return success(
        await user_profile_service.get_my_profile(session, user_id=context.user_id)
    )
profile = await user_profile_service.update_my_profile(
    session, payload, user_id=context.user_id, context=context
)
return success(profile, message="updated")
```

**Why the API layer:** the spec requires an empty `PATCH {}` to produce **no audit record**, and `audited` is bound to the service function, so any call to the decorated service writes a success entry. The empty-body check is a routing decision about whether this request is a mutation, which is interface semantics, not domain logic. Do not move it into the service and do not weaken the test to "an audit row is fine as long as nothing was written".

Then register the route metadata at module bottom (the spec requires it; without it dependency-layer denials are not audited):

```python
register_audit_route(
    "PATCH", "/me/profile", action="user_profile:update", resource_type="user_profile"
)
```

The path is the router-prefixed template (`/me/profile`), matching how `app/api/system_settings.py` registers `"/system-settings"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_user_profile_service.py tests/test_profile_api.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/user_profile.py app/api/profile.py tests/test_user_profile_service.py tests/test_profile_api.py
git commit -m "feat: add profile update endpoint"
```

---

### Task 5: `PUT /api/v1/me/profile/privacy`

**Files:**
- Modify: `app/services/user_profile.py`
- Modify: `app/api/profile.py`
- Test: `tests/test_profile_api.py`

**Interfaces:**
- Consumes: `PrivacyUpdate`, `PUBLIC_PROFILE_FIELD_KEYS`, the lazy-creation path from Task 4.
- Produces: `replace_privacy(session, payload, *, user_id: int, context: AuthContext) -> ProfileRead` with `@audited(action="user_profile:privacy", resource_type="user_profile", resource_id_param="user_id")`; `PUT "/privacy"` route.

- [ ] **Step 1: Write the failing tests**

- `{"hidden_fields": ["avatar", "signature"]}` → 200, and the stored value is `["signature", "avatar"]` (canonical `PUBLIC_PROFILE_FIELD_KEYS` order, not submission order).
- `{"hidden_fields": ["signature", "signature"]}` stores `["signature"]`.
- `{"hidden_fields": ["nickname"]}` → 422; `["department_name"]` → 422.
- `{"hidden_fields": []}` clears all hiding.
- One audit row with `action="user_profile:privacy"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_profile_api.py -q`
Expected: FAIL — 405 on `PUT`

- [ ] **Step 3: Implement the service**

Validate the whitelist **before any write**: `unknown = set(payload.hidden_fields) - set(PUBLIC_PROFILE_FIELD_KEYS)`; if non-empty raise `BusinessError(ErrorCode.INVALID_PARAMETER, "Unknown profile field", http_status=422)`. Normalize with `[key for key in PUBLIC_PROFILE_FIELD_KEYS if key in set(payload.hidden_fields)]` — dedupe and canonical order in one expression. Reuse Task 4's lazy-creation path for a missing row, then commit and return `get_my_profile(...)`.

`nickname` and `department_name` are not in the whitelist by design: name and department are the minimum needed to find a person.

- [ ] **Step 4: Implement the endpoint**

`PUT "/privacy"` with `response_model=SuccessEnvelope[ProfileRead]`; register `register_audit_route("PUT", "/me/profile/privacy", action="user_profile:privacy", resource_type="user_profile")`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_profile_api.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/user_profile.py app/api/profile.py tests/test_profile_api.py
git commit -m "feat: add profile privacy endpoint"
```

---

### Task 6: Avatar Upload and Delete

**Files:**
- Modify: `app/core/rate_limit.py`
- Modify: `app/services/system_setting_asset.py`
- Create: `app/services/user_avatar.py`
- Modify: `app/api/profile.py`
- Test: `tests/test_user_avatar.py`

**Interfaces:**
- Consumes: `_read_bounded`, `_detect_image_format`, `_validate_declared_content_type`, `_store_atomic`, `_managed_asset_path`, `run_best_effort`, `enforce_rate_limit`.
- Produces: `AVATAR_UPLOAD_WINDOW_SECONDS`, `AVATAR_UPLOAD_MAX_REQUESTS`, `avatar_upload_rate_limit_key(user_id) -> str`; `upload_user_avatar(*, content_type, chunks) -> str`; `remove_user_avatar(url: str | None) -> bool`; `upload_avatar(session, *, user_id, content_type, chunks, context) -> str`; `delete_avatar(session, *, user_id, context) -> None`; `AvatarUploadRead`; `POST "/avatar"` and `DELETE "/avatar"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_user_avatar.py`, reusing the `media_root` fixture shape from `tests/test_system_setting_asset.py` (`monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))`) and its `PNG`/`JPEG`/`WEBP`/`ICO` constants. Assert:

- Upload PNG with `Content-Type: image/png` → 200, `data["url"]` starts with `/media/user/avatar/`, filename is a UUID plus `.png`, file bytes equal the request body, `user.avatar` updated.
- **Upload a valid PNG with no `Content-Type` header → 200.** This pins the shared primitive's existing behaviour so nobody later "fixes" it into a stricter rule that would change system-setting uploads too.
- `Content-Type: image/jpeg` with PNG bytes → 422, `media_root` has no files, `user.avatar` unchanged.
- Bytes matching no supported format → 422, nothing on disk.
- Body over `settings.system_asset_max_bytes` → 422, nothing on disk.
- Replacing an existing managed avatar → old file gone, new file present, `user.avatar` points at the new URL.
- Existing **external** avatar URL (`https://example.com/a.png`) → upload succeeds, no attempt to delete the external URL, no error.
- `DELETE` → 200, `data["url"] is None`, `user.avatar is None`, managed file removed.
- The 11th upload within the window → 429 with `code == 1005` (use the `fake_redis` fixture from `tests/conftest.py`).
- Audit: one each for `user_profile:avatar_upload` and `user_profile:avatar_delete`, `resource_id == user_id`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_user_avatar.py -q`
Expected: FAIL — 404 on `/api/v1/me/profile/avatar`

- [ ] **Step 3: Add the rate-limit primitives**

`app/core/rate_limit.py`:

```python
AVATAR_UPLOAD_WINDOW_SECONDS = 3600
AVATAR_UPLOAD_MAX_REQUESTS = 10


def avatar_upload_rate_limit_key(user_id: int) -> str:
    return key("user_profile", "avatar_upload", str(user_id))
```

Add all three to `__all__`. **Do not reuse `tianyancha_rate_limit_key`** — that name is upstream-vendor-specific. The limit exists because the upload endpoint is open to every active employee, unlike the admin-only system-setting upload, so one account could otherwise fill the disk with 2 MiB files.

- [ ] **Step 4: Generalize the asset storage primitive**

In `app/services/system_setting_asset.py`:

- Introduce the relative directory list `("system/logo", "system/favicon", "user/avatar")` as the single source of truth for both URL construction and managed-path matching.
- `_asset_url` must take a full relative directory (`system/logo`, `user/avatar`, …) instead of only a `SystemAssetType`. Keep `SystemAssetType` working by mapping it to `f"system/{value}"` internally. **Every existing `/media/system/logo/` URL and every assertion in `tests/test_system_setting_asset.py` must remain byte-identical.**
- `_managed_asset_path` stops hardcoding the `/system/` prefix: iterate the directory list and match a prefix. Keep every existing guard — UUID stem, suffix in `.png/.jpg/.webp/.ico`, `Path(filename).name == filename`, and `candidate.parent == directory`.
- Add `upload_user_avatar(*, content_type, chunks) -> str` with the same flow as `upload_system_asset` (`_read_bounded` → `_detect_image_format` → `_validate_declared_content_type` → `_store_atomic` → `_asset_url("user/avatar", filename)`), but **without** `@audited` — the service layer records the audit, and decorating both would double-log.
- Add `remove_user_avatar(url) -> bool` reusing `_managed_asset_path`.

- [ ] **Step 5: Implement the avatar service**

`app/services/user_avatar.py`:

- `upload_avatar`: fetch the `User` (missing → 404), remember `previous_url = user.avatar`, `url = await upload_user_avatar(...)`, `user.avatar = url`, `await session.commit()`, then `await run_best_effort(lambda: remove_user_avatar(previous_url), description="remove superseded user avatar")`. Do **not** pre-check whether `previous_url` is managed — `remove_user_avatar` already returns `False` for external URLs and for `None`, so letting it decide keeps one rule in one place.
- `delete_avatar`: fetch the `User`, `user.avatar = None`, `commit`, then best-effort remove the previous URL.

**Ordering is a hard constraint: commit first, delete the file after.** A delete that fails only logs; it must never fail the request. Deleting before the commit would orphan a deleted-file-with-live-URL row if the commit then failed.

- [ ] **Step 6: Implement the endpoints**

`POST "/avatar"` injects `redis: Annotated[Redis, Depends(get_redis)]` and calls `enforce_rate_limit(redis, (avatar_upload_rate_limit_key(context.user_id),), window_seconds=AVATAR_UPLOAD_WINDOW_SECONDS, max_requests=AVATAR_UPLOAD_MAX_REQUESTS, message="Too many avatar uploads; wait before retrying")` **before** the service call, so a rejected request never touches disk. Read the body from `request.stream()` and the header from `request.headers.get("content-type")`. `DELETE "/avatar"` returns `{"url": None}`. Both use `response_model=SuccessEnvelope[AvatarUploadRead]`, where `AvatarUploadRead(BaseModel)` has exactly one field, `url: str | None`, and lives in `app/schemas/profile.py`. Register both routes with `register_audit_route`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_user_avatar.py tests/test_system_setting_asset.py -q`
Expected: PASS. The second file passing is the evidence that generalizing the primitive did not change system-setting behaviour.

- [ ] **Step 8: Commit**

```bash
git add app/core/rate_limit.py app/services/system_setting_asset.py app/services/user_avatar.py app/api/profile.py tests/test_user_avatar.py
git commit -m "feat: add user avatar upload and delete"
```

---

### Task 7: Directory List and Department Filter Options

**Files:**
- Modify: `app/repositories/user_profile.py`
- Create: `app/schemas/directory.py`
- Create: `app/services/directory.py`
- Create: `app/api/directory.py`
- Modify: `app/api/router.py`
- Modify: `app/main.py`
- Modify: `tests/test_app_boot.py`
- Test: `tests/test_directory_service.py`
- Test: `tests/test_directory_api.py`

**Interfaces:**
- Consumes: `department_repository.list_all`, `User`, `UserProfile`, `Department`, `Page`, `MAX_PAGE_SIZE`.
- Produces: `DirectoryRow` (NamedTuple); `list_directory_entries(...)`; `display_name(nickname, username, user_id) -> str`; `clip_scalar(...)`; `clip_list(...)`; `build_directory_entry(row) -> DirectoryEntryRead`; `list_directory(...)`; `list_directory_departments(...)`; `DirectoryEntryRead`, `DirectoryDepartmentRead`; `router` with `prefix="/directory"`.

- [ ] **Step 1: Write the failing service tests**

`tests/test_directory_service.py`:

- Active users (`status=active`, `deleted_at IS NULL`) appear; `disabled` and soft-deleted users do not.
- `keyword` matches `nickname` (one case) and `username` (another).
- `department_id` is exact: filtering by a parent department does **not** include child-department employees.
- `display_name`: equals `nickname` when set; falls back to `username` when `nickname is None`; falls back to `f"用户{user_id}"` when both are `None` (a Feishu-provisioned user before rename).
- Sort order: `department.sort_order`, then `display_name`, then `id`; users with no department sort last.
- A soft-deleted department still yields its name: the employee appears with `department_name` equal to the original name. A user with no department has `department_name is None`.
- `hidden_public_fields` containing `signature` makes that row's `signature` `None`; containing `avatar` makes `avatar` `None`. **The list clips too, not just the detail view.**
- Pagination stability: 5 users sharing department and display name, `page_size=2`; the three pages are disjoint and cover all 5.

- [ ] **Step 2: Write the failing API tests**

`tests/test_directory_api.py`:

- `GET /api/v1/directory` with an **empty** `permission_codes` `AuthContext` → 200 (proves no `user:read` is needed).
- `GET /api/v1/directory/departments` → 200, includes non-deleted, non-system, active departments, excludes the `is_system=True` "待分配" department and disabled departments, ordered by `sort_order` then `id`.
- Each list item's key set is **exactly** `{"id", "display_name", "nickname", "avatar", "signature", "department_name"}` — assert with `set(item) == {...}` so a future private field cannot slip in.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_directory_service.py tests/test_directory_api.py -q`
Expected: FAIL — `ModuleNotFoundError: app.services.directory`

- [ ] **Step 4: Implement the schemas**

`app/schemas/directory.py`: `DirectoryEntryRead` with `id: int`, `display_name: str`, `nickname: str | None`, `avatar: str | None`, `signature: str | None`, `department_name: str | None`; `DirectoryDepartmentRead` with `id: int`, `name: str`, `parent_id: int | None`, `sort_order: int`.

- [ ] **Step 5: Implement the list query**

`app/repositories/user_profile.py` adds `DirectoryRow` and `list_directory_entries(session, *, page, page_size, keyword, department_id) -> tuple[list[DirectoryRow], int]`.

Spec §15 lists a `get_many_by_user_ids` repository helper. **Do not add it.** It exists in that sketch for a design where the directory fetched users first and then stitched in profiles; the join query below returns everything in one round trip, so a batch-by-ids helper would be dead code. The spec marks its file list as guidance, not a hard constraint.

Query: `select(...).select_from(User).outerjoin(UserProfile, UserProfile.user_id == User.id).outerjoin(Department, Department.id == User.department_id)`, filtered by `User.deleted_at.is_(None)` and `User.status == UserStatus.ACTIVE`, plus an equality filter on `User.department_id` and an `or_(User.nickname.like(...), User.username.like(...))` keyword filter when provided.

**Do not add `Department.deleted_at.is_(None)` or a status filter** — neither to the selected columns nor to the sort. "Person still employed, department gone" must still render a name. This is a deliberate asymmetry with `GET /directory/departments` in the same task, which does filter to active, non-deleted departments: the filter list picks a valid department, the list itself shows every active person.

Sorting: `case((Department.sort_order.is_(None), 1), else_=0)`, then `Department.sort_order`, then `func.coalesce(User.nickname, User.username)`, then `User.id`. MySQL has no `NULLS LAST`, hence the `case`. The trailing `User.id` is what makes pagination stable.

Count with the same conditions on a `select(func.count()).select_from(User).outerjoin(...)` — both joins are 1:1 (`user_profile.user_id` is unique), so the count cannot be inflated.

- [ ] **Step 6: Implement the clipping module**

`app/services/directory.py` is the **only** place in the project that implements colleague-view clipping; Task 8 reuses it. Split the helper by value shape rather than writing one generic truthiness check:

```python
def display_name(nickname: str | None, username: str | None, user_id: int) -> str
def clip_scalar(value: str | None, *, key: str, hidden: list[str]) -> str | None
def clip_list[T](value: list[T], *, key: str, hidden: list[str]) -> list[T] | None
```

`clip_list` returns `None` only when the key is hidden, otherwise the original list — an unhidden empty list stays `[]`, because the spec requires `[]` and not `null` for empty list fields. A single `if not value: return None` helper would wrongly collapse `[]` to `None` and break the Task 8 contract.

Also add `list_directory(...)` and `list_directory_departments(session)`. The latter uses `department_repository.list_all(session, include_disabled=False)` (it already filters `deleted_at` and `status`) and then drops `is_system` entries in memory.

- [ ] **Step 7: Implement the endpoints**

`app/api/directory.py`: `router = APIRouter(prefix="/directory", tags=["通讯录"])`, no `require_permission` anywhere. `GET ""` returns `SuccessEnvelope[Page[DirectoryEntryRead]]` with `page` (`ge=1`), `page_size` (`ge=1, le=MAX_PAGE_SIZE`, default 20), `keyword`, `department_id`. `GET "/departments"` returns the filter list.

Mount the router in `app/api/router.py`. Add `{"name": "通讯录", "description": "在职员工目录"}` to `OPENAPI_TAGS` and `"通讯录"` to the expected set in `tests/test_app_boot.py`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_directory_service.py tests/test_directory_api.py tests/test_app_boot.py -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add app/repositories/user_profile.py app/schemas/directory.py app/services/directory.py app/api/directory.py app/api/router.py app/main.py tests/test_app_boot.py tests/test_directory_service.py tests/test_directory_api.py
git commit -m "feat: add employee directory list and department filters"
```

---

### Task 8: `GET /api/v1/directory/{user_id}` — Colleague Profile

**Files:**
- Modify: `app/schemas/directory.py`
- Modify: `app/repositories/user_profile.py`
- Modify: `app/services/directory.py`
- Modify: `app/api/directory.py`
- Test: `tests/test_directory_service.py`
- Test: `tests/test_directory_api.py`

**Interfaces:**
- Consumes: `display_name`, `clip_scalar`, `clip_list` (Task 7).
- Produces: `DirectoryProfileRow`; `get_directory_profile_row(session, user_id)`; `build_directory_profile(row) -> DirectoryProfileRead`; `get_directory_profile(session, user_id)`; `DirectoryProfileRead`; `GET "/{user_id}"` route declared **after** `/departments`.

- [ ] **Step 1: Write the failing tests**

- An active employee → 200 with `id`, `display_name`, `nickname`, `department_name`, `status`, and all ten business-card keys.
- The business-card key set is **exactly** `{"avatar", "signature", "bio", "work_phone", "extension", "work_email", "office_location", "seat", "expertise_tags", "personal_links"}` — no more, no fewer.
- Colleague has `hidden_public_fields=["bio"]` and a non-empty `bio` → `bio is None` while `seat` still returns its value.
- A never-filled `bio` (`NULL`) also yields `bio is None`; assert the two responses are indistinguishable on `bio` and that the key exists in both.
- `expertise_tags` / `personal_links` that are unhidden but empty return `[]`, **not** `None`.
- The four private fields (`personal_phone`, `birthday`, `emergency_contact_name`, `emergency_contact_phone`) are absent from the key set.
- A `disabled` or soft-deleted employee → 404 with `code == 2000`.
- Requesting one's own `user_id` → 200, clipped by the colleague rules (a field you hid is `None` for you too, which lets the frontend preview the colleague view).
- `GET /api/v1/directory/999999999` → 404, proving `departments` is not being swallowed as a path parameter.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_directory_service.py tests/test_directory_api.py -q`
Expected: FAIL — 404 / no route

- [ ] **Step 3: Implement the response model**

`DirectoryProfileRead` in `app/schemas/directory.py`: `id`, `display_name`, `nickname`, `department_name`, `status`, plus all ten business-card fields as nullable (lists as `list[str] | None` / `list[PersonalLink] | None`). Import `PersonalLink` from `app.schemas.profile` — the same shape is used by `ProfileRead.personal_links`, so define it once in Task 2 and reuse it rather than creating a second link model. **No `Field(exclude=True)`, no `response_model_exclude_none`** — every key always appears so "hidden" and "unfilled" are indistinguishable on the wire.

`status` here is the raw `UserStatus` enum (the colleague view needs to show whether the person is active), serialized as `"active"`. It is not the same concept as the private-zone fields and is intentionally always visible.

- [ ] **Step 4: Implement the row query**

`get_directory_profile_row(session, user_id) -> DirectoryProfileRow | None` in the repository, selecting nickname, username, all business-card fields, `hidden_public_fields`, `department_name`, and `status`, filtered by `User.id == user_id`, `User.deleted_at.is_(None)`, `User.status == UserStatus.ACTIVE`. Same no-department-filter rule as Task 7.

- [ ] **Step 5: Implement the service**

`build_directory_profile(row)` reuses `display_name`, `clip_scalar` (8 nullable scalars), and `clip_list` (`expertise_tags`, `personal_links`). **Do not reimplement the clipping rules here.** `get_directory_profile` raises `BusinessError(ErrorCode.USER_NOT_FOUND, "User not found", http_status=404)` when the row is missing.

- [ ] **Step 6: Implement the endpoint**

`GET "/{user_id}"` with `user_id: int` and `response_model=SuccessEnvelope[DirectoryProfileRead]`, declared **after** `"/departments"`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_directory_service.py tests/test_directory_api.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/schemas/directory.py app/repositories/user_profile.py app/services/directory.py app/api/directory.py tests/test_directory_service.py tests/test_directory_api.py
git commit -m "feat: add colleague directory profile endpoint"
```

---

### Task 9: Feishu Avatar Backfills Only When Locally Empty

**Files:**
- Modify: `app/services/auth.py:453-465`
- Test: `tests/test_feishu_login.py`

**Interfaces:**
- Consumes: existing `_sync_feishu_profile`.
- Produces: no new symbols; one changed branch.

- [ ] **Step 1: Confirm the current behaviour is as described**

Run: `.venv/bin/pytest tests/test_feishu_login.py -q`
Expected: PASS — `test_avatar_and_last_login_always_refreshed` currently asserts that every login overwrites the avatar.

- [ ] **Step 2: Update the tests**

Rename `test_avatar_and_last_login_always_refreshed` to `test_avatar_is_only_backfilled_when_empty` and change the avatar assertion to `logged_in.avatar == "https://example.com/old.png"` (the locally stored one wins). Keep every `last_login_at` assertion exactly as it is — `last_login_at` must still refresh on every login, with `tzinfo is None` and `<= datetime.now(UTC).replace(tzinfo=None)`.

Add `test_avatar_is_backfilled_when_locally_empty`: create the user with `avatar=None`, have Feishu return `avatar_url="https://example.com/first.png"`, assert `logged_in.avatar == "https://example.com/first.png"`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_feishu_login.py -q`
Expected: FAIL — the renamed test, because the avatar is still overwritten

- [ ] **Step 4: Change the branch**

In `_sync_feishu_profile`, replace

```python
    if info.avatar_url:
        user.avatar = info.avatar_url
```

with

```python
    if not user.avatar and info.avatar_url:
        user.avatar = info.avatar_url
```

The neighboring `nickname` and `email` lines already use `if not user.X and info.X`; this makes the third field consistent. Leave `user.last_login_at = login_at` untouched. As a consequence, clearing an avatar (via `DELETE /me/profile/avatar`) allows the next Feishu login to refill it — that is the intended reading of "clearing a field lets it be refilled", not a bug.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_feishu_login.py -q`
Expected: PASS, including `test_empty_feishu_avatar_does_not_clear_stored_avatar`, whose semantics are unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/services/auth.py tests/test_feishu_login.py
git commit -m "fix: only backfill the Feishu avatar when no local avatar is set"
```

---

### Task 10: Directory Menu Seed and Baseline Menu Grant

**Files:**
- Modify: `scripts/init_data.py`
- Test: `tests/test_seed_data.py`

**Interfaces:**
- Consumes: `_MENU_SEEDS`, `_get_or_create_menus`, `_find_menu`, `Role`, `RoleMenu`.
- Produces: `_DIRECTORY_MENU_NAME`; `_get_or_create_menus` returning `tuple[dict[str, Menu], set[str]]`; `_grant_baseline_menus(session, *, menus_by_key, created_keys)` replacing `_ensure_default_role_menu`.

- [ ] **Step 1: Confirm the baseline**

Run: `.venv/bin/pytest tests/test_seed_data.py -q`
Expected: PASS

- [ ] **Step 2: Update the three assertions this change breaks**

The spec names only one of these. All three must change:

- `test_init_creates_super_admin_and_default_roles:128` — `assert await _count(session, RoleMenu) == 1` becomes `== 2`, because the `default` role now holds two menus. Leave lines 129-134 alone: `super_admin` must still have **zero** `role_menu` rows.
- `test_init_is_idempotent_when_run_twice:477,479` — `assert await _count(session, Menu) == 37` becomes `== 38`, and `RoleMenu == 1` becomes `== 2`. Keep these as literal counts: the test's value is proving a second run changes nothing, and the totals are the current seed snapshot. Do not weaken them into a vague "count unchanged" comparison.
- `test_default_role_is_scoped_to_self:137-157` — replace the single-scalar comparison with a set of menu names: `{"个人资料", "通讯录"}`, and keep `assert default.data_scope == DataScope.SELF`. **Use `list((await session.scalars(...)).all())`, not `session.scalar(...)`** — `scalar` returns only the first row and would silently downgrade the assertion to "any one menu matches".

Two things deliberately **not** changed: `test_init_creates_baseline_menu_tree` derives its assertions from the real tree, so a new top-level permission-less `MENU` does not break it; and `EXPECTED_PERMISSION_CODES` stays as it is because this design adds no permission code.

- [ ] **Step 3: Add the new seed test**

`test_directory_menu_is_seeded_once_and_granted_to_existing_roles`: run `await init_data(...)` twice, then assert

- exactly one top-level menu named `通讯录`, with `type == MenuType.MENU`, `path == "/directory"`, `permission_code is None`, `sort_order == 5`;
- the `default` role's menu-name set is still exactly `{"个人资料", "通讯录"}` after the second run (no duplicate insert, no re-grant side effect).

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_seed_data.py -q`
Expected: FAIL — no `通讯录` menu

- [ ] **Step 5: Add the menu seed**

Add `_DIRECTORY_MENU_NAME = "通讯录"` beside the existing `_PROFILE_MENU_NAME`. Insert into `_MENU_SEEDS` **before** the `profile` entry:

```python
(
    _MenuSeed(
        key="directory",
        parent_key=None,
        menu_type=MenuType.MENU,
        name=_DIRECTORY_MENU_NAME,
        path="/directory",
        sort_order=5,
    ),
)
```

`sort_order=5` places it ahead of `个人资料` (10). No `permission_code` — the directory is open to every logged-in employee.

- [ ] **Step 6: Make `_get_or_create_menus` report what it created**

Change the return type to `tuple[dict[str, Menu], set[str]]`, accumulate `created_keys.add(seed.key)` inside the `if menu is None:` branch, and return `(menus_by_key, created_keys)`.

The created-keys set is the whole point: only menus created *in this run* get granted. Granting on every run would mean that an admin who unchecks the menu in the role editor has it silently re-added on the next deploy — an unwinnable loop.

- [ ] **Step 7: Replace the single-menu grant with the baseline grant**

Delete `_ensure_default_role_menu` and add `_grant_baseline_menus(session, *, menus_by_key, created_keys)`: build `targets = [menus_by_key[k] for k in ("profile", "directory") if k in created_keys]`; return early if empty; select role ids with `Role.deleted_at.is_(None)` **and `Role.code != _SUPER_ADMIN_ROLE_CODE`**; for each `(role_id, menu.id)` insert into `RoleMenu` only if the row does not already exist.

**Excluding `super_admin` deliberately departs from the spec's literal "insert a row for every role with `deleted_at IS NULL`".** The reason is a pre-existing invariant: `tests/test_seed_data.py:129-134` asserts `super_admin` has no `role_menu` rows at all, because its visibility comes from the `is_super_admin` shortcut in `get_visible_menu_tree`, not from the join table. Inserting rows for it is useless and breaks that invariant. The spec itself concludes this batch grant is "equivalent to granting only `default`" on a fresh database; excluding the super admin is what makes that sentence literally true. The alternative — relaxing lines 129-134 — would weaken a meaningful constraint to accommodate a do-nothing row.

In `init_data`, unpack both return values and call `_grant_baseline_menus(...)` instead of `_ensure_default_role_menu(...)`. The `default_role` local is then unused; drop the assignment rather than leaving a dead variable.

- [ ] **Step 8: Verify the documented coverage limits hold**

Run: `.venv/bin/pytest tests/test_seed_data.py tests/test_e2e_permission_flow.py -q`
Expected: PASS. `test_e2e_permission_flow` creates its role *after* `init_data`, when both menus already exist, so no baseline grant fires; it grants only `user:read` and should still see `{系统管理, 用户管理}` with the two assertions intact. **If it does not, stop and report to the user — do not relax the assertions to get green.** The spec explicitly asks for this to be confirmed by running, not by reasoning.

Then verify the two accepted limitations are still true and note them in the commit body: pre-existing deployments will not back-grant the menu to existing custom roles, and roles created after this release will not see it until an admin ticks it.

- [ ] **Step 9: Commit**

```bash
git add scripts/init_data.py tests/test_seed_data.py
git commit -m "feat: seed the directory menu and grant baseline menus on first creation"
```

---

### Task 11: Full Regression

**Files:** none — verification only.

**Interfaces:**
- Consumes: everything above.
- Produces: a green tree and a checked acceptance list.

- [ ] **Step 1: Verify the migration and static analysis**

```bash
DATABASE_URL=... alembic upgrade head && alembic current
.venv/bin/ruff check .
.venv/bin/mypy .
```

Expected: one head; both linters clean (`mypy` runs `strict = true`).

- [ ] **Step 2: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: everything passes except the 2 known-failing `tests/test_audit_denial_hooks.py` cases that only fail under full-suite ordering pollution. Investigate any other failure individually. **Do not close out by editing assertions or skipping tests.**

- [ ] **Step 3: Prove the two known failures are unrelated**

Run: `.venv/bin/pytest tests/test_audit_denial_hooks.py -q`
Expected: `7 passed` — they pass in isolation on HEAD, so they are cross-test pollution, not this feature.

- [ ] **Step 4: Run the privilege-regression check**

Spec §12 requires that this design does not widen `GET /users` visibility and does not extend what `PATCH /users/{id}` accepts:

Run: `.venv/bin/pytest tests/test_user_crud.py tests/test_user_audit.py tests/test_privilege_escalation.py tests/test_e2e_permission_flow.py -q`
Expected: PASS with **no test edits**. `UserRead`/`UserUpdate` still expose only `nickname`/`email`/`avatar`, and the admin field set is deliberately not extended (admins cannot edit another employee's card fields — an accepted limitation recorded in the spec). If any of these files needed a change to pass, that is a scope violation, not a test fix.

- [ ] **Step 5: Walk the spec's acceptance criteria**

Open spec §13 and, for each of its 11 criteria, point at the test that proves it. Add a test for any criterion without one. In particular confirm these are covered somewhere: every active employee can read and write their own profile without `user:read`/`user:update`; hiding a field makes it `null` for colleagues; keyword search by name and login name; every directory row has a non-empty display name; disabled/deleted employees are neither listed nor openable; no private field appears in any colleague response; avatar survives a subsequent Feishu login; profile edits are audited with no field values; the migration upgrades and downgrades without rewriting `user` rows.

- [ ] **Step 6: Check the commit history is clean**

Run: `git log --oneline -12 && git status --porcelain`
Expected: conventional-commit messages, and no probe scripts, temp files, or `.env` changes staged. The two untracked docs — `docs/superpowers/specs/2026-10-18-user-profile-space-design.md` and this plan — should be committed as well (`git add docs/superpowers/specs docs/superpowers/plans && git commit -m "docs: add profile space spec and implementation plan"`).

- [ ] **Step 7: Commit any added coverage**

```bash
git add tests/
git commit -m "test: cover remaining profile and directory acceptance criteria"
```
