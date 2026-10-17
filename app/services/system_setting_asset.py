"""系统设置图片资源的安全存储。"""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from app.core.auth_context import AuthContext
from app.core.config import (
    normalize_media_url_path,
    resolve_media_root,
    settings,
)
from app.core.errors import BusinessError, ErrorCode
from app.services.audit import audited


class SystemAssetType(StrEnum):
    LOGO = "logo"
    FAVICON = "favicon"


class _ImageFormat(NamedTuple):
    content_type: str
    extension: str


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_RIFF_SIGNATURE = b"RIFF"
_WEBP_SIGNATURE = b"WEBP"
_ICO_SIGNATURE = b"\x00\x00\x01\x00"

_PNG_FORMAT = _ImageFormat("image/png", ".png")
_JPEG_FORMAT = _ImageFormat("image/jpeg", ".jpg")
_WEBP_FORMAT = _ImageFormat("image/webp", ".webp")
_ICO_FORMAT = _ImageFormat("image/x-icon", ".ico")

_ALLOWED_DECLARED_CONTENT_TYPES = {
    "image/png": frozenset({"image/png"}),
    "image/jpeg": frozenset({"image/jpeg", "image/jpg"}),
    "image/webp": frozenset({"image/webp"}),
    "image/x-icon": frozenset({"image/x-icon", "image/vnd.microsoft.icon"}),
}

_INVALID_IMAGE_MESSAGE = "Image must be a valid PNG, JPEG, WebP, or ICO file"


def _invalid_image(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.INVALID_PARAMETER,
        message,
        http_status=422,
    )


def _detect_image_format(content: bytes) -> _ImageFormat | None:
    if content.startswith(_PNG_SIGNATURE):
        return _PNG_FORMAT
    if content.startswith(_JPEG_SIGNATURE):
        return _JPEG_FORMAT
    if (
        len(content) >= 12
        and content.startswith(_RIFF_SIGNATURE)
        and content[8:12] == _WEBP_SIGNATURE
    ):
        return _WEBP_FORMAT
    if len(content) >= 6 and content.startswith(_ICO_SIGNATURE):
        return _ICO_FORMAT
    return None


def _normalize_content_type(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def _validate_declared_content_type(
    content_type: str | None,
    detected: _ImageFormat,
) -> None:
    if content_type is None:
        return
    normalized = _normalize_content_type(content_type)
    if normalized not in _ALLOWED_DECLARED_CONTENT_TYPES[detected.content_type]:
        raise _invalid_image("Content-Type does not match the image data")


async def _read_bounded(
    chunks: AsyncIterator[bytes],
    max_bytes: int,
) -> bytes:
    content = bytearray()
    limit = max_bytes + 1
    async for chunk in chunks:
        remaining = limit - len(content)
        if remaining > 0:
            content.extend(chunk[:remaining])
        if len(content) > max_bytes:
            raise _invalid_image(f"Image exceeds the {max_bytes}-byte limit")
    return bytes(content)


def _write_atomic(directory: Path, filename: str, content: bytes) -> None:
    temporary_path: Path | None = None
    target_path = directory / filename
    published = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{filename}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(target_path)
        published = True
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if published:
            target_path.unlink(missing_ok=True)
        raise


def _store_atomic(directory: Path, filename: str, content: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_atomic(directory, filename, content)


def _asset_url(asset_type: SystemAssetType, filename: str) -> str:
    base_path = normalize_media_url_path(settings.media_url_path)
    return f"{base_path}/system/{asset_type.value}/{filename}"


def _managed_asset_path(url: str | None) -> Path | None:
    if url is None or not url.startswith("/"):
        return None

    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None

    prefix = f"{normalize_media_url_path(settings.media_url_path)}/system/"
    if not parsed.path.startswith(prefix):
        return None

    segments = parsed.path.removeprefix(prefix).split("/")
    if len(segments) != 2:
        return None
    asset_type_value, filename = segments
    try:
        asset_type = SystemAssetType(asset_type_value)
        UUID(Path(filename).stem)
    except ValueError:
        return None
    if Path(filename).name != filename:
        return None
    if Path(filename).suffix not in {".png", ".jpg", ".webp", ".ico"}:
        return None

    directory = (
        resolve_media_root(settings.media_root) / "system" / asset_type.value
    ).resolve()
    candidate = (directory / filename).resolve()
    if candidate.parent != directory:
        return None
    return candidate


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


async def remove_system_asset(url: str | None) -> bool:
    """删除由系统生成且路径安全的本地资源文件。"""
    path = _managed_asset_path(url)
    if path is None:
        return False
    await asyncio.to_thread(_unlink, path)
    return True


@audited(
    action="system_setting:asset_upload",
    resource_type="system_setting",
)
async def upload_system_asset(
    *,
    asset_type: SystemAssetType,
    content_type: str | None,
    chunks: AsyncIterator[bytes],
    context: AuthContext,
) -> str:
    content = await _read_bounded(chunks, settings.system_asset_max_bytes)
    detected = _detect_image_format(content)
    if detected is None:
        raise _invalid_image(_INVALID_IMAGE_MESSAGE)
    _validate_declared_content_type(content_type, detected)

    directory = resolve_media_root(settings.media_root) / "system" / asset_type.value
    filename = f"{uuid4()}{detected.extension}"
    await asyncio.to_thread(_store_atomic, directory, filename, content)
    return _asset_url(asset_type, filename)


__all__: tuple[str, ...] = (
    "SystemAssetType",
    "remove_system_asset",
    "upload_system_asset",
)
