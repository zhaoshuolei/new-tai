import os
import stat
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import app.main as main
from app.core.auth_context import AuthContext
from app.core.config import Settings, settings
from app.core.errors import BusinessError, ErrorCode
from app.models.role import DataScope
from app.services import audit as audit_service
from app.services.system_setting_asset import (
    SystemAssetType,
    remove_system_asset,
    upload_system_asset,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00"
WEBP = b"RIFF\x04\x00\x00\x00WEBP"
ICO = b"\x00\x00\x01\x00\x01\x00"


class CapturingAuditSession:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def __aenter__(self) -> CapturingAuditSession:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        return None

    def add(self, row: Any) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.fixture(autouse=True)
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    rows: list[Any] = []
    monkeypatch.setattr(
        audit_service,
        "session_factory",
        lambda: CapturingAuditSession(rows),
    )
    return rows


@pytest.fixture
def context() -> AuthContext:
    return AuthContext(
        user_id=42,
        role_id=3,
        department_id=None,
        data_scope=DataScope.ALL,
        custom_department_ids=frozenset(),
        include_children=True,
        permission_codes=frozenset({"system_setting:update"}),
        is_super_admin=False,
        username="settings-admin",
        nickname="Settings Admin",
    )


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "media"
    monkeypatch.setattr(settings, "media_root", str(root))
    return root


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def assert_no_files(root: Path) -> None:
    assert not root.exists() or not any(root.rglob("*"))


def settings_with(test_settings: Settings, **overrides: Any) -> Settings:
    return Settings(
        **{**test_settings.model_dump(), **overrides},
        _env_file=None,
    )


@pytest.mark.parametrize(
    ("asset_type", "content_type", "payload", "extension"),
    [
        (SystemAssetType.LOGO, "image/png", PNG, ".png"),
        (SystemAssetType.FAVICON, "image/jpeg", JPEG, ".jpg"),
        (SystemAssetType.LOGO, "image/webp", WEBP, ".webp"),
        (SystemAssetType.FAVICON, "image/x-icon", ICO, ".ico"),
    ],
)
async def test_uploads_each_supported_image_type_from_bytes(
    asset_type: SystemAssetType,
    content_type: str,
    payload: bytes,
    extension: str,
    media_root: Path,
    context: AuthContext,
    audit_rows: list[Any],
) -> None:
    url = await upload_system_asset(
        asset_type=asset_type,
        content_type=content_type,
        chunks=chunks(payload, b""),
        context=context,
    )

    prefix = f"/media/system/{asset_type.value}/"
    assert url.startswith(prefix)
    stored_name = url.removeprefix(prefix)
    assert stored_name.endswith(extension)
    UUID(Path(stored_name).stem)
    stored_path = media_root / "system" / asset_type.value / stored_name
    assert stored_path.read_bytes() == payload
    assert len(audit_rows) == 1


async def test_missing_declared_content_type_uses_detected_format(
    media_root: Path,
    context: AuthContext,
) -> None:
    url = await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type=None,
        chunks=chunks(WEBP),
        context=context,
    )

    assert url.endswith(".webp")
    assert (media_root / "system" / "logo" / Path(url).name).read_bytes() == WEBP


async def test_successful_upload_records_audit_entry(
    media_root: Path,
    context: AuthContext,
    audit_rows: list[Any],
) -> None:
    await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG),
        context=context,
    )

    assert len(audit_rows) == 1
    assert audit_rows[0].action == "system_setting:asset_upload"
    assert audit_rows[0].resource_type == "system_setting"
    assert audit_rows[0].resource_id is None
    assert audit_rows[0].user_id == context.user_id
    assert audit_rows[0].actor_username == context.username
    assert audit_rows[0].actor_nickname == context.nickname


async def test_filesystem_write_runs_off_event_loop_thread(
    media_root: Path,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_thread = threading.current_thread()
    operation_threads: list[tuple[str, threading.Thread]] = []
    original_mkdir = Path.mkdir
    original_mkstemp = tempfile.mkstemp
    original_fsync = os.fsync
    original_replace = Path.replace

    def record_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        operation_threads.append(("mkdir", threading.current_thread()))
        original_mkdir(path, *args, **kwargs)

    def record_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        operation_threads.append(("mkstemp", threading.current_thread()))
        return original_mkstemp(*args, **kwargs)

    def record_fsync(descriptor: int) -> None:
        operation_threads.append(("fsync", threading.current_thread()))
        original_fsync(descriptor)

    def record_replace(path: Path, target: Path) -> Path:
        operation_threads.append(("replace", threading.current_thread()))
        return original_replace(path, target)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    monkeypatch.setattr(tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(Path, "replace", record_replace)

    url = await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG),
        context=context,
    )

    stored_path = media_root / "system" / "logo" / Path(url).name
    assert stored_path.read_bytes() == PNG
    assert {operation for operation, _thread in operation_threads} == {
        "mkdir",
        "mkstemp",
        "fsync",
        "replace",
    }
    assert all(thread is not main_thread for _operation, thread in operation_threads)
    assert len({thread.ident for _operation, thread in operation_threads}) == 1


async def test_remove_system_asset_deletes_only_managed_files(
    media_root: Path,
) -> None:
    managed = media_root / "system" / "logo" / f"{UUID(int=1)}.png"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(PNG)
    outside = media_root / "system" / "secret.png"
    outside.write_bytes(PNG)

    removed = await remove_system_asset(f"/media/system/logo/{managed.name}")
    traversal_removed = await remove_system_asset("/media/system/logo/../secret.png")
    external_removed = await remove_system_asset("https://cdn.example.com/logo.png")

    assert removed is True
    assert managed.exists() is False
    assert traversal_removed is False
    assert outside.exists() is True
    assert external_removed is False


async def test_atomic_write_fsyncs_destination_directory(
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_directory = False
    original_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        nonlocal synced_directory
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced_directory = True
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG),
        context=context,
    )

    assert synced_directory is True


async def test_exact_maximum_size_is_accepted(
    media_root: Path,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "system_asset_max_bytes", len(PNG) + 1)

    await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG, b"\x00"),
        context=context,
    )

    stored_files = [path for path in media_root.rglob("*") if path.is_file()]
    assert sum(path.stat().st_size for path in stored_files) == len(PNG) + 1


async def test_one_byte_over_maximum_is_rejected_without_files(
    media_root: Path,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
    audit_rows: list[Any],
) -> None:
    monkeypatch.setattr(settings, "system_asset_max_bytes", len(PNG))

    with pytest.raises(BusinessError) as error:
        await upload_system_asset(
            asset_type=SystemAssetType.LOGO,
            content_type="image/png",
            chunks=chunks(PNG, b"\x00"),
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert_no_files(media_root)
    assert audit_rows == []


async def test_declared_content_type_must_match_detected_type(
    media_root: Path,
    context: AuthContext,
    audit_rows: list[Any],
) -> None:
    with pytest.raises(BusinessError) as error:
        await upload_system_asset(
            asset_type=SystemAssetType.LOGO,
            content_type="image/png",
            chunks=chunks(JPEG),
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert_no_files(media_root)
    assert audit_rows == []


@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("image/png", b"not-an-image"),
        ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        (None, b"not-an-image"),
    ],
)
async def test_malformed_or_unsupported_images_are_rejected_without_files(
    content_type: str | None,
    payload: bytes,
    media_root: Path,
    context: AuthContext,
    audit_rows: list[Any],
) -> None:
    with pytest.raises(BusinessError) as error:
        await upload_system_asset(
            asset_type=SystemAssetType.LOGO,
            content_type=content_type,
            chunks=chunks(payload),
            context=context,
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER
    assert error.value.http_status == 422
    assert_no_files(media_root)
    assert audit_rows == []


async def test_uploads_produce_different_uuid_filenames(
    media_root: Path,
    context: AuthContext,
) -> None:
    first = await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG),
        context=context,
    )
    second = await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG),
        context=context,
    )

    assert first != second
    assert Path(first).name != Path(second).name


def test_asset_storage_configuration_defaults(test_settings: Settings) -> None:
    assert Path(test_settings.media_root) == (Path.cwd() / "media").resolve()
    assert test_settings.media_url_path == "/media"
    assert test_settings.system_asset_max_bytes == 2 * 1024 * 1024


def test_relative_media_root_is_resolved_when_settings_are_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
) -> None:
    configured = settings_with(test_settings, media_root="media")
    resolved = (Path.cwd() / "media").resolve()

    assert Path(configured.media_root) == resolved

    monkeypatch.chdir(tmp_path)
    assert Path(configured.media_root) == resolved


def test_asset_storage_maximum_must_be_positive(test_settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings_with(test_settings, system_asset_max_bytes=0)


@pytest.mark.parametrize(
    "unsafe_media_root",
    [
        "",
        "   ",
        ".",
        "..",
        str(Path.cwd()),
        str(Path.cwd().parent),
    ],
)
def test_settings_reject_empty_or_ancestor_media_roots(
    test_settings: Settings,
    unsafe_media_root: str,
) -> None:
    with pytest.raises(ValidationError, match="media_root"):
        settings_with(test_settings, media_root=unsafe_media_root)


@pytest.mark.parametrize(
    ("media_url_path", "canonical_path"),
    [
        ("media", "/media"),
        ("/media/", "/media"),
        ("/configured//media/", "/configured/media"),
    ],
)
def test_settings_canonicalize_media_url_path(
    test_settings: Settings,
    media_url_path: str,
    canonical_path: str,
) -> None:
    configured = settings_with(test_settings, media_url_path=media_url_path)

    assert configured.media_url_path == canonical_path


@pytest.mark.parametrize("unsafe_media_url_path", ["", "   ", "/", "///"])
def test_settings_reject_empty_or_root_media_url_path(
    test_settings: Settings,
    unsafe_media_url_path: str,
) -> None:
    with pytest.raises(ValidationError, match="media_url_path"):
        settings_with(test_settings, media_url_path=unsafe_media_url_path)


@pytest.mark.parametrize(
    "unsafe_media_root",
    ["", ".", "..", str(Path.cwd()), str(Path.cwd().parent)],
)
def test_app_rejects_unsafe_media_root_before_static_mount(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_media_root: str,
) -> None:
    mounted_directories: list[str] = []

    class CapturingStaticFiles:
        def __init__(self, *, directory: str, check_dir: bool) -> None:
            mounted_directories.append(directory)
            assert check_dir is False

    monkeypatch.setattr(settings, "media_root", unsafe_media_root)
    monkeypatch.setattr(main, "StaticFiles", CapturingStaticFiles)

    with pytest.raises(ValueError, match="media_root"):
        main.create_app()

    assert mounted_directories == []


@pytest.mark.parametrize("unsafe_media_url_path", ["", " ", "/", "///"])
def test_app_rejects_empty_or_root_media_url_path_before_static_mount(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_media_url_path: str,
) -> None:
    created_static_handlers: list[str] = []

    class CapturingStaticFiles:
        def __init__(self, *, directory: str, check_dir: bool) -> None:
            created_static_handlers.append(directory)
            assert check_dir is False

    monkeypatch.setattr(settings, "media_url_path", unsafe_media_url_path)
    monkeypatch.setattr(main, "StaticFiles", CapturingStaticFiles)

    with pytest.raises(ValueError, match="media_url_path"):
        main.create_app()

    assert created_static_handlers == []


async def test_uploaded_url_uses_canonical_media_path(
    media_root: Path,
    context: AuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "media_url_path", "//configured//media/")

    url = await upload_system_asset(
        asset_type=SystemAssetType.LOGO,
        content_type="image/png",
        chunks=chunks(PNG),
        context=context,
    )

    assert url.startswith("/configured/media/system/logo/")


async def test_app_creates_missing_media_root_for_static_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "not-yet-created"
    monkeypatch.setattr(settings, "media_root", str(media_root))
    application = main.create_app()

    assert media_root.is_dir()

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/media/system/logo/missing.png")

    assert response.status_code == 404


async def test_static_media_is_served_from_patched_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "configured-media"
    stored = media_root / "system" / "logo" / "existing.png"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(PNG)
    monkeypatch.setattr(settings, "media_root", str(media_root))
    monkeypatch.setattr(settings, "media_url_path", "//configured-media//nested/")
    application = main.create_app()

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/configured-media/nested/system/logo/existing.png")
        write_response = await client.post(
            "/configured-media/nested/system/logo/existing.png",
            content=PNG,
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert write_response.status_code == 405
