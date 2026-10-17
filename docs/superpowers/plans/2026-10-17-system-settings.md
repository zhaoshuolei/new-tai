# System Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent, permission-controlled system settings resource with public branding reads and safe Logo/Favicon uploads.

**Architecture:** Add a singleton-style `system_setting` row behind repository, service, schema, and API layers. Public branding reads are unauthenticated; admin reads, updates, and uploads use new menu permissions. Uploaded images are streamed into a configurable local media root and served statically without adding a multipart parser.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, MySQL, Alembic, pytest

**Spec:** `.scratch/system-settings/spec.md`

## Global Constraints

- Do not commit unless the user explicitly asks.
- This repository delivers only the backend; the admin page and public consumption are frontend work.
- Support exactly these editable fields: name, short_name, site_url, logo_url, favicon_url, description, copyright, icp_number.
- `site_url` is required on PUT, allows URL paths, and accepts only absolute HTTP/HTTPS URLs without credentials.
- Accept only PNG, JPEG, WebP, and ICO uploads; limit each upload to 2 MiB; never trust filename or client-declared type. SVG is excluded.
- Uploads use raw request bodies, not multipart; return `{"url": "..."}` and audit them as `system_setting:asset_upload`.
- Use `system_setting:read` and `system_setting:update` permission codes. Only the super administrator receives them implicitly; existing other roles do not.
- Do not delete replaced or unsaved assets in this release.
- Keep `init_data` idempotent and preserve the existing default-role behavior.
- Keep source ASCII except for established Chinese user-facing menu names and docstrings.

## Review Focus

- Malformed and oversized bodies must not leave files on disk.
- URL validation must reject schemes other than HTTP/HTTPS, missing hosts, and embedded credentials while accepting paths.
- PUT must reject a missing site URL and omission must clear optional fields because PUT replaces the full editable object.
- Concurrent first writes must not create duplicate global settings rows.
- A public read must work before initialization and must not expose audit metadata.
- Successful uploads must be audited even though their returned URL is not yet saved into the active settings.
- Existing non-super-admin roles must remain without the new permissions.

---

### Task 1: Persistent Settings Model and Repository

**Files:**
- Create: `app/models/system_setting.py`
- Create: `app/repositories/system_setting.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/<generated>_create_system_setting.py`
- Test: `tests/test_system_setting_repository.py`

**Interfaces:**
- Consumes: `Base` timestamp/id columns and the existing async-session patterns.
- Produces: `SystemSetting`; `get_global(session) -> SystemSetting | None`; `upsert_global(session, values: dict[str, object], *, updated_by: int | None) -> SystemSetting`.

- [ ] **Step 1: Write repository tests**

Cover a missing row, inserting all values, updating the same row, and the
concurrent-insert recovery path by monkeypatching the first lookup to return
`None` while a row already exists. Assert exactly one `setting_key == "global"`
row remains and `updated_by` changes.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../../.venv/bin/pytest tests/test_system_setting_repository.py -q`

Expected: collection/import failure because the model and repository do not exist.

- [ ] **Step 3: Implement the model and repository**

Define a `setting_key` unique non-null string plus the eight editable strings,
`updated_by`, and inherited audit columns. Mirror the savepoint-plus-lock
recovery used by `integration_config.upsert`, using `with_for_update()` on the
recovery read. Flush and refresh but do not commit in the repository.

- [ ] **Step 4: Generate and inspect the migration**

Run:

```bash
../../.venv/bin/alembic revision --autogenerate -m "create system setting"
../../.venv/bin/alembic upgrade head
../../.venv/bin/alembic downgrade -1
../../.venv/bin/alembic upgrade head
```

Expected: only `system_setting` is added; upgrade, downgrade, and re-upgrade all
succeed. Do not stage or commit the revision file.

- [ ] **Step 5: Run the repository tests**

Run: `../../.venv/bin/pytest tests/test_system_setting_repository.py -q`

Expected: PASS.

---

### Task 2: Settings Schemas and Service

**Files:**
- Create: `app/schemas/system_setting.py`
- Create: `app/services/system_setting.py`
- Test: `tests/test_system_setting_service.py`

**Interfaces:**
- Consumes: `get_global` and `upsert_global` from Task 1.
- Produces: `SystemSettingUpdate`, `SystemSettingRead`, `PublicSystemSettingRead`; `get_public_setting(session) -> PublicSystemSettingRead`; `get_admin_setting(session) -> SystemSettingRead`; `update_setting(session, payload, *, context) -> SystemSetting`.

`site_url` is required in `SystemSettingUpdate`; the public read can still return
`null` before the first form save.

- [ ] **Step 1: Write failing schema and service tests**

Test required nonblank names, length boundaries, optional blank-to-null
normalization, HTTP/HTTPS site URLs, rejection of credentials and non-HTTP
schemes, relative and absolute asset URLs, default reads before initialization,
full update persistence, `updated_by`, and an audit row for
`system_setting:update`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../../.venv/bin/pytest tests/test_system_setting_service.py -q`

Expected: import failure for the new schema and service modules.

- [ ] **Step 3: Implement schemas and service**

Use Pydantic validators for URL rules and trimming. `SystemSettingUpdate` has
`extra="forbid"`, requires `name` and `site_url`, and defaults every other
optional field to `None` so omission clears it. Public reads use
`settings.app_name` as the fallback name.
The update service strips the required name, rejects a blank result with
`BusinessError(ErrorCode.INVALID_PARAMETER, ..., 422)`, writes all fields,
commits, and is decorated with:

```python
@audited(
    action="system_setting:update",
    resource_type="system_setting",
    resource_id_attr="id",
)
```

- [ ] **Step 4: Run the service tests**

Run: `../../.venv/bin/pytest tests/test_system_setting_service.py -q`

Expected: PASS.

---

### Task 3: Safe Asset Storage and Static Serving

**Files:**
- Create: `app/services/system_setting_asset.py`
- Modify: `app/core/config.py`
- Modify: `app/main.py`
- Modify: `.env.example`
- Modify: `.gitignore`
- Test: `tests/test_system_setting_asset.py`

**Interfaces:**
- Consumes: runtime `settings.media_root`, `settings.media_url_path`, and `settings.system_asset_max_bytes`.
- Produces: `SystemAssetType`; `upload_system_asset(*, asset_type: SystemAssetType, content_type: str | None, chunks: AsyncIterator[bytes], context: AuthContext) -> str`.

- [ ] **Step 1: Write failing storage tests**

Exercise valid PNG, JPEG, WebP, and ICO signatures; exact maximum size; one byte
over the maximum; a type mismatch between declared and detected content type;
malformed bytes; and two uploads producing different filenames. Use
`tmp_path`, monkeypatch `settings.media_root`, and supply an `AuthContext`.
Assert rejected uploads leave no files and successful uploads record
`system_setting:asset_upload` when the audit-session factory is patched.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../../.venv/bin/pytest tests/test_system_setting_asset.py -q`

Expected: import failure for the asset service.

- [ ] **Step 3: Implement bounded streaming and magic-byte detection**

Read chunks until EOF or `max_bytes + 1`. Detect signatures for PNG, JPEG,
WebP, and ICO, normalize the detected MIME/extension, require `Content-Type` to
match when supplied, make directories with `mkdir(parents=True, exist_ok=True)`,
and write an atomic temporary file followed by `replace`. Build URLs from the
sanitized `media_url_path`, asset type, and UUID filename. Decorate the public
service entry point with:

```python
@audited(
    action="system_setting:asset_upload",
    resource_type="system_setting",
)
```

- [ ] **Step 4: Add configuration and static mounting**

Add `media_root: str = "media"`, `media_url_path: str = "/media"`, and
`system_asset_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1)`. Mount
`StaticFiles(directory=settings.media_root, check_dir=False)` at the normalized
media path. Add the settings to `.env.example` and ignore the local media root.

- [ ] **Step 5: Run the storage and app boot tests**

Run:

```bash
../../.venv/bin/pytest tests/test_system_setting_asset.py tests/test_app_boot.py tests/test_config.py -q
```

Expected: PASS, including static file serving from a patched temporary root.

---

### Task 4: API Routes, Menu Permissions, and Initialization

**Files:**
- Create: `app/api/system_settings.py`
- Modify: `app/api/router.py`
- Modify: `app/main.py`
- Modify: `scripts/init_data.py`
- Modify: `tests/test_seed_data.py`
- Test: `tests/test_system_setting_api.py`

**Interfaces:**
- Consumes: Task 2 settings services and Task 3 asset service.
- Produces: public GET, protected admin GET, protected PUT, and protected raw-body POST routes under `/api/v1/system-settings`.

- [ ] **Step 1: Write failing API tests**

Verify the public route succeeds without auth and omits metadata. Verify admin
GET and PUT return 403 without `system_setting:read`/`system_setting:update`,
success with permissions, full replace semantics, audit registration, and
upload validation. Assert upload return shape is `data: {"url": "..."}`.

- [ ] **Step 2: Run the API tests to verify they fail**

Run: `../../.venv/bin/pytest tests/test_system_setting_api.py -q`

Expected: 404 responses for the new routes.

- [ ] **Step 3: Implement the API router**

Use `request.stream()` for uploads and an enum path parameter restricted to
`logo`/`favicon`. Register `PUT /system-settings` with
`register_audit_route`, add the `系统设置` OpenAPI tag, and include the router in
the v1 aggregate.

- [ ] **Step 4: Extend initialization and seed tests**

Add menu seeds for `system_setting:read` at `/system/settings` and
`system_setting:update` as its child button. Seed the global settings row with
`settings.app_name`. Update expected permission sets and exact menu/row counts;
run initialization twice and assert both the settings row and new menus remain
unique.

- [ ] **Step 5: Run focused API and seed tests**

Run:

```bash
../../.venv/bin/pytest tests/test_system_setting_api.py tests/test_seed_data.py tests/test_e2e_permission_flow.py -q
```

Expected: PASS.

---

### Task 5: Full Verification and Review

**Files:**
- Modify only files required by failures found in this task.
- Review: the complete diff from `master`.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: verified branch state; no commit.

- [ ] **Step 1: Run formatting and lint checks**

Run:

```bash
../../.venv/bin/ruff format --check app tests scripts
../../.venv/bin/ruff check app tests scripts
```

Expected: no formatting or lint errors.

- [ ] **Step 2: Run strict type checking**

Run: `../../.venv/bin/mypy app`

Expected: success with no errors.

- [ ] **Step 3: Re-run migration round trip against the test database**

Run:

```bash
../../.venv/bin/alembic current
../../.venv/bin/alembic upgrade head
../../.venv/bin/alembic downgrade -1
../../.venv/bin/alembic upgrade head
```

Expected: head is the new migration and all operations succeed.

- [ ] **Step 4: Run the full suite**

Run: `../../.venv/bin/pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Inspect the final diff**

Run:

```bash
git status --short
git diff --check
git diff master --stat
git diff master -- . ':!uv.lock'
```

Expected: no whitespace errors, no unrelated changes, and no secrets or generated
media files. Report any checks that could not run.
