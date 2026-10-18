from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.services import data_import_storage as storage

JSON_CONTENT = b'{"name":"alice"}'
JSON_SHA256 = "3b8f02c64624e355de637e609642b441ab1427b619d9fb91cb6c7b0e8f8ceed1"


def _configure_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_bytes: int = 52428800,
) -> Path:
    root = tmp_path / "data-imports"
    monkeypatch.setattr(settings, "data_import_root", str(root))
    monkeypatch.setattr(settings, "data_import_max_bytes", max_bytes)
    return root


def test_save_import_bytes_writes_random_name_and_exact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)

    stored = storage.save_import_bytes(
        JSON_CONTENT,
        suffix=".json",
        original_name="../../source.json",
    )
    stored_path = root / stored.storage_path

    assert stored_path.parent == root
    assert stored_path.read_bytes() == JSON_CONTENT
    assert stored.storage_path == stored_path.name
    assert UUID(stored_path.stem)
    assert stored.original_name == "source.json"
    assert stored.size_bytes == len(JSON_CONTENT)
    assert stored.content_sha256 == JSON_SHA256
    assert "source" not in stored.storage_path


@pytest.mark.parametrize(
    ("original_name", "expected"),
    [
        ("../../source.json", "source.json"),
        ("source\r\n\x00.json", "source.json"),
        ("x" * 400, "x" * 255),
        ("\r\n\t\x00", None),
        ("", None),
    ],
)
def test_save_import_bytes_sanitizes_original_name_for_downstream_metadata(
    original_name: str,
    expected: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)

    stored = storage.save_import_bytes(
        JSON_CONTENT,
        suffix=".json",
        original_name=original_name,
    )

    assert stored.original_name == expected
    filename, suffix = stored.storage_path.rsplit(".", maxsplit=1)
    assert suffix == "json"
    assert UUID(filename)
    assert (root / stored.storage_path).is_file()


def test_save_import_bytes_generates_distinct_uuids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)

    first = storage.save_import_bytes(JSON_CONTENT, suffix=".json")
    second = storage.save_import_bytes(JSON_CONTENT, suffix=".json")

    assert first.storage_path != second.storage_path
    assert (root / first.storage_path).read_bytes() == JSON_CONTENT
    assert (root / second.storage_path).read_bytes() == JSON_CONTENT


def test_save_import_bytes_accepts_exact_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_storage(tmp_path, monkeypatch, max_bytes=len(JSON_CONTENT))

    stored = storage.save_import_bytes(JSON_CONTENT, suffix=".json")

    assert stored.size_bytes == len(JSON_CONTENT)


def test_save_import_bytes_rejects_one_byte_over_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch, max_bytes=4)
    root.mkdir()

    with pytest.raises(BusinessError) as error:
        storage.save_import_bytes(b"12345", suffix=".json")

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert error.value.http_status == 422
    assert list(root.iterdir()) == []


def test_save_import_bytes_rejects_unsupported_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)

    with pytest.raises(BusinessError) as error:
        storage.save_import_bytes(b"name\n", suffix=".csv")  # type: ignore[arg-type]

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert not root.exists() or list(root.iterdir()) == []


def test_save_import_bytes_cleans_up_temporary_file_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        storage.save_import_bytes(JSON_CONTENT, suffix=".json")

    assert list(root.iterdir()) == []


def test_save_import_bytes_cleans_up_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)
    real_fsync = storage.os.fsync
    fsync_calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(storage.os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        storage.save_import_bytes(JSON_CONTENT, suffix=".json")

    assert fsync_calls == 2
    assert list(root.iterdir()) == []


def test_read_and_delete_import_source_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)
    stored = storage.save_import_bytes(JSON_CONTENT, suffix=".json")
    stored_path = root / stored.storage_path

    with storage.read_import_source(stored.storage_path) as source:
        assert source.read() == JSON_CONTENT

    storage.delete_import_source(stored.storage_path)
    storage.delete_import_source(stored.storage_path)

    assert not stored_path.exists()


def test_read_import_source_reports_safe_missing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_storage(tmp_path, monkeypatch)

    with pytest.raises(BusinessError) as error:
        storage.read_import_source("missing.json")

    assert error.value.code == ErrorCode.DATA_IMPORT_NOT_FOUND
    assert error.value.http_status == 404


@pytest.mark.parametrize(
    "operation", [storage.read_import_source, storage.delete_import_source]
)
def test_read_and_delete_reject_traversal_and_absolute_paths(
    operation: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(JSON_CONTENT)

    for unsafe_path in ("../outside.json", str(outside)):
        with pytest.raises(BusinessError) as error:
            operation(unsafe_path)  # type: ignore[operator]

        assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
        assert error.value.http_status == 422

    assert outside.read_bytes() == JSON_CONTENT


@pytest.mark.parametrize(
    "operation", [storage.read_import_source, storage.delete_import_source]
)
def test_read_and_delete_reject_symlink_escape(
    operation: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(JSON_CONTENT)
    (root / "linked.json").symlink_to(outside)

    with pytest.raises(BusinessError) as error:
        operation("linked.json")  # type: ignore[operator]

    assert error.value.code == ErrorCode.DATA_IMPORT_INVALID
    assert outside.read_bytes() == JSON_CONTENT
