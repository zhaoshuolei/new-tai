"""Private storage for uploaded data import source files."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Literal, NamedTuple
from uuid import uuid4

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.services.private_storage import sanitize_original_name

ImportSuffix = Literal[".xlsx", ".json"]

_ALLOWED_SUFFIXES: frozenset[str] = frozenset({".xlsx", ".json"})
_WRITE_CHUNK_SIZE = 64 * 1024
_INVALID_IMPORT_MESSAGE = "Data import source is invalid"


class StoredImportSource(NamedTuple):
    storage_path: str
    original_name: str | None
    size_bytes: int
    content_sha256: str


def _invalid_import(message: str = _INVALID_IMPORT_MESSAGE) -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_INVALID,
        message,
        http_status=422,
    )


def _import_not_found() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_NOT_FOUND,
        "Data import source was not found",
        http_status=404,
    )


def _import_root() -> Path:
    return Path(settings.data_import_root).expanduser().resolve()


def _write_atomic(directory: Path, filename: str, content: bytes) -> str:
    temporary_path: Path | None = None
    target_path = directory / filename
    published = False
    digest = hashlib.sha256()
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{filename}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary_file:
            for offset in range(0, len(content), _WRITE_CHUNK_SIZE):
                chunk = content[offset : offset + _WRITE_CHUNK_SIZE]
                digest.update(chunk)
                temporary_file.write(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target_path)
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
    return digest.hexdigest()


def _resolve_import_path(storage_path: str) -> Path:
    relative_path = Path(storage_path)
    if relative_path.is_absolute() or not relative_path.parts:
        raise _invalid_import()
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        raise _invalid_import()

    root = _import_root()
    try:
        candidate = (root / relative_path).resolve()
    except OSError, RuntimeError, ValueError:
        raise _invalid_import() from None
    if candidate == root or not candidate.is_relative_to(root):
        raise _invalid_import()
    return candidate


def save_import_bytes(
    content: bytes,
    *,
    suffix: ImportSuffix,
    original_name: str | None = None,
) -> StoredImportSource:
    if suffix not in _ALLOWED_SUFFIXES:
        raise _invalid_import()
    if len(content) > settings.data_import_max_bytes:
        raise _invalid_import(
            f"Data import source exceeds the "
            f"{settings.data_import_max_bytes}-byte limit"
        )

    root = _import_root()
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4()}{suffix}"
    content_sha256 = _write_atomic(root, filename, content)
    return StoredImportSource(
        storage_path=filename,
        original_name=sanitize_original_name(original_name),
        size_bytes=len(content),
        content_sha256=content_sha256,
    )


def read_import_source(storage_path: str) -> BinaryIO:
    path = _resolve_import_path(storage_path)
    if not path.is_file():
        raise _import_not_found()
    try:
        return path.open("rb")
    except FileNotFoundError:
        raise _import_not_found() from None


def delete_import_source(storage_path: str) -> None:
    path = _resolve_import_path(storage_path)
    if not path.exists():
        return
    if not path.is_file():
        raise _invalid_import()
    try:
        path.unlink()
    except FileNotFoundError:
        return


__all__: tuple[str, ...] = (
    "StoredImportSource",
    "delete_import_source",
    "read_import_source",
    "save_import_bytes",
)
