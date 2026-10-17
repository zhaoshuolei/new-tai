# System Settings Design

## Intent

Provide one backend-managed source of truth for public site identity so the
frontend and login surfaces no longer depend on hard-coded branding or
deployment-only environment values.

## Delivery Boundary

This repository implements the backend only: data persistence, API, permissions,
menu seed data, audit records, and media serving. The admin settings page and
public frontend consumption live in the frontend repository.

## Scope

The first release manages these fields:

- Website name: required, trimmed, 1-100 characters.
- Short name: optional, trimmed, maximum 50 characters.
- Website URL: required when saving the settings form. It must be an absolute
  `http` or `https` URL without embedded credentials; URL paths are allowed.
- Logo URL: optional; either an absolute `http`/`https` URL or a site-relative
  path beginning with `/`.
- Favicon URL: optional, with the same validation as Logo URL.
- Website description: optional, trimmed, maximum 500 characters.
- Copyright text: optional, trimmed, maximum 200 characters.
- ICP filing number: optional, trimmed, maximum 100 characters.

Logo and favicon uploads support PNG, JPEG, WebP, and ICO. Uploads are limited
to 2 MiB. SVG is deliberately excluded because this backend has no XML
sanitizer and serving active SVG from the API origin creates a script-execution
risk. File type is determined from bytes rather than filename or client MIME
type.

## Data Model

`system_setting` is a singleton-style table with a unique `setting_key` whose
only supported value is `global`. It stores the eight public fields plus
`updated_by` and the standard timestamps. Keeping the singleton key explicit
makes accidental duplicate rows impossible while avoiding assumptions about a
particular primary key value.

`init_data` idempotently creates the global row with the website name defaulted
to `settings.app_name`; all optional fields, including the as-yet-unconfirmed
site URL, start empty. A missing row during runtime also reads as defaults so an
uninitialized deployment still has safe public configuration. Saving requires
the site URL even though bootstrap can expose a null value before first save.

## API

- `GET /api/v1/system-settings/public`: unauthenticated read used by login and
  public frontend surfaces. It returns only the eight public fields.
- `GET /api/v1/system-settings`: requires `system_setting:read`; returns the
  public fields plus `updated_at` and `updated_by`.
- `PUT /api/v1/system-settings`: requires `system_setting:update`; validates all
  public fields, persists the global row, and records an audit event.
- `POST /api/v1/system-settings/assets/{asset_type}`: requires
  `system_setting:update`; accepts `logo` or `favicon` in the path and raw image
  bytes as the request body. The declared `Content-Type` must match the detected
  file format. It stores the image, records `system_setting:asset_upload`, and
  returns a site-relative URL. Saving the returned URL through the PUT endpoint
  is a separate step, so cancelling an edit does not change the active
  configuration. Raw bodies avoid adding a multipart parser dependency.

All responses use the existing `{code, message, data}` envelope. The public
settings object is:

```json
{
  "name": "tianxiadmin",
  "short_name": null,
  "site_url": null,
  "logo_url": null,
  "favicon_url": null,
  "description": null,
  "copyright": null,
  "icp_number": null
}
```

The admin read adds `updated_at` and `updated_by`. The PUT body contains every
field above except `updated_at`/`updated_by`: `name` is required, and omitted or
blank optional values are stored and returned as `null`. PUT therefore replaces
the full editable settings object. Asset upload returns
`data: {"url": "/media/system/logo/<uuid>.png"}`.

Invalid URLs, blank required names, malformed images, unsupported types, and
oversized files return HTTP 422 with `INVALID_PARAMETER`.

## Asset Storage

Uploaded files are stored under `<MEDIA_ROOT>/system/<asset_type>/` with random
UUID filenames. `<MEDIA_URL_PATH>` is mounted as read-only static content.
Defaults are `MEDIA_ROOT=media`, `MEDIA_URL_PATH=/media`, and a 2 MiB upload
limit. Client filenames are never used for storage, preventing traversal and
extension spoofing. When a saved local logo or favicon is replaced, the old
managed file is deleted on a best-effort basis after the settings commit. Files
uploaded for an edit that is never saved remain until a future garbage
collection job; persistence is deliberately decoupled from an unsaved form.

Local storage is the supported first-release deployment mode. Production must
run a single API instance or mount the same durable `MEDIA_ROOT` on every
replica. Otherwise object storage is required before scaling out. The service
does not decode or resize images, so the 2 MiB byte cap is the only server-side
resource limit in this release.

## Permissions and Menu

Seed one menu and one button under `系统管理`:

- `系统设置`, path `/system/settings`, permission `system_setting:read`,
  sort order 90.
- `保存系统设置`, permission `system_setting:update`, child of the menu.

The super administrator receives these permissions through its existing
enabled-menu behavior. Existing roles other than the super administrator must
be granted them explicitly; initialization does not broaden existing roles.

## Non-goals

- SMTP, SMS, registration switches, maintenance mode, security policy, or
  arbitrary key/value settings.
- Cloud object storage or CDN integration.
- Automatic deletion or garbage collection of replaced or unsaved assets.
- Multipart upload support.
- Frontend implementation; this repository contains only the backend.

## Acceptance Criteria

- Public branding can be read without authentication.
- Admin reads and writes require their documented permissions.
- A valid image upload creates a uniquely named file, records an audit entry,
  and returns a public URL.
- Invalid, unsupported, or oversized uploads are rejected without creating a
  file.
- Successful updates persist all fields, the updater, and an audit record.
- Existing roles do not gain settings permissions implicitly.
- The migration upgrades and downgrades cleanly.
- Existing initialization remains idempotent and now seeds exactly one global
  settings row and the two new permission records.
