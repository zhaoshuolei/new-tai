"""跟进记录附件的格式识别与私有存储。"""

import asyncio
import io
import os
import tempfile
import unicodedata
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode


class AttachmentFormat(NamedTuple):
    content_type: str
    extension: str


class StoredAttachment(NamedTuple):
    storage_path: str
    content_type: str
    size_bytes: int


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_RIFF_SIGNATURE = b"RIFF"
_WEBP_SIGNATURE = b"WEBP"
_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURE = b"PK\x03\x04"

_PNG_FORMAT = AttachmentFormat("image/png", ".png")
_JPEG_FORMAT = AttachmentFormat("image/jpeg", ".jpg")
_WEBP_FORMAT = AttachmentFormat("image/webp", ".webp")
_PDF_FORMAT = AttachmentFormat("application/pdf", ".pdf")
_DOCX_FORMAT = AttachmentFormat(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docx",
)
_XLSX_FORMAT = AttachmentFormat(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsx",
)

_ALLOWED_DECLARED_CONTENT_TYPES: dict[str, frozenset[str]] = {
    "image/png": frozenset({"image/png"}),
    "image/jpeg": frozenset({"image/jpeg", "image/jpg"}),
    "image/webp": frozenset({"image/webp"}),
    "application/pdf": frozenset({"application/pdf"}),
    _DOCX_FORMAT.content_type: frozenset({_DOCX_FORMAT.content_type}),
    _XLSX_FORMAT.content_type: frozenset({_XLSX_FORMAT.content_type}),
}

_INVALID_ATTACHMENT_MESSAGE = "Attachment is invalid or unsupported"
_ATTACHMENT_DIRECTORY = "contact-follow-up"


def _invalid_attachment(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID,
        message,
        http_status=422,
    )


def _detect_ooxml_format(content: bytes) -> AttachmentFormat | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
    except Exception:
        return None

    if "[Content_Types].xml" not in names:
        return None
    if any(name.startswith("word/") for name in names):
        return _DOCX_FORMAT
    if any(name.startswith("xl/") for name in names):
        return _XLSX_FORMAT
    return None


def detect_attachment_format(content: bytes) -> AttachmentFormat | None:
    """仅根据文件字节识别受支持的附件格式。"""
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
    if content.startswith(_PDF_SIGNATURE):
        return _PDF_FORMAT
    if content.startswith(_ZIP_SIGNATURE):
        return _detect_ooxml_format(content)
    return None


def _normalize_content_type(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def _validate_declared_content_type(
    content_type: str | None,
    detected: AttachmentFormat,
) -> None:
    if content_type is None:
        return
    normalized = _normalize_content_type(content_type)
    if normalized not in _ALLOWED_DECLARED_CONTENT_TYPES[detected.content_type]:
        raise _invalid_attachment(_INVALID_ATTACHMENT_MESSAGE)


def sanitize_original_name(value: str | None) -> str | None:
    """移除控制字符和路径前缀，返回可安全展示的原始文件名。"""
    if value is None:
        return None

    without_controls = "".join(
        character for character in value if unicodedata.category(character) != "Cc"
    )
    last_segment = without_controls.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not last_segment:
        return None
    return last_segment[:255]


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
            raise _invalid_attachment(f"Attachment exceeds the {max_bytes}-byte limit")
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


def resolve_attachment_path(storage_path: str) -> Path | None:
    """将相对存储路径解析为私有根目录内的现有普通文件。"""
    relative_path = Path(storage_path)
    if relative_path.is_absolute():
        return None

    root = Path(settings.follow_up_attachment_root).expanduser().resolve()
    try:
        candidate = (root / relative_path).resolve()
    except OSError, RuntimeError, ValueError:
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


async def store_attachment(
    *,
    follow_up_id: int,
    content_type: str | None,
    chunks: AsyncIterator[bytes],
) -> StoredAttachment:
    """识别附件字节并原子写入私有附件目录。"""
    content = await _read_bounded(
        chunks,
        settings.follow_up_attachment_max_bytes,
    )
    detected = detect_attachment_format(content)
    if detected is None:
        raise _invalid_attachment(_INVALID_ATTACHMENT_MESSAGE)
    _validate_declared_content_type(content_type, detected)

    filename = f"{uuid4()}{detected.extension}"
    directory = (
        Path(settings.follow_up_attachment_root).expanduser()
        / _ATTACHMENT_DIRECTORY
        / str(follow_up_id)
    )
    await asyncio.to_thread(_store_atomic, directory, filename, content)
    return StoredAttachment(
        storage_path=f"{_ATTACHMENT_DIRECTORY}/{follow_up_id}/{filename}",
        content_type=detected.content_type,
        size_bytes=len(content),
    )


async def remove_attachment_file(storage_path: str) -> bool:
    """删除安全解析到私有目录内的附件文件。

    路径必须解析为现有普通文件才返回 True；``missing_ok`` 用于处理解析后、
    删除前文件恰好消失的竞态。
    """
    path = resolve_attachment_path(storage_path)
    if path is None:
        return False
    await asyncio.to_thread(path.unlink, missing_ok=True)
    return True


__all__: tuple[str, ...] = (
    "AttachmentFormat",
    "StoredAttachment",
    "detect_attachment_format",
    "remove_attachment_file",
    "resolve_attachment_path",
    "sanitize_original_name",
    "store_attachment",
)
