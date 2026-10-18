import io
import threading
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.services import follow_up_attachment_storage as storage

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"
JPEG = b"\xff\xd8\xff\xe0" + b"rest"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ooxml(main_part: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(main_part, "<part/>")
    return buffer.getvalue()


DOCX = _ooxml("word/document.xml")
XLSX = _ooxml("xl/workbook.xml")


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.mark.parametrize(
    ("content", "content_type", "extension"),
    [
        (PNG, "image/png", ".png"),
        (JPEG, "image/jpeg", ".jpg"),
        (WEBP, "image/webp", ".webp"),
        (PDF, "application/pdf", ".pdf"),
        (DOCX, DOCX_CONTENT_TYPE, ".docx"),
        (XLSX, XLSX_CONTENT_TYPE, ".xlsx"),
    ],
)
def test_detect_supported_formats(
    content: bytes,
    content_type: str,
    extension: str,
) -> None:
    detected = storage.detect_attachment_format(content)

    assert detected is not None
    assert detected.content_type == content_type
    assert detected.extension == extension


def test_plain_zip_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")

    assert storage.detect_attachment_format(buffer.getvalue()) is None


def test_malformed_zip_and_unsupported_content_are_rejected() -> None:
    assert storage.detect_attachment_format(b"PK\x03\x04broken") is None
    assert storage.detect_attachment_format(b"not-an-attachment") is None


def test_ooxml_package_with_only_a_word_directory_entry_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/", b"")

    assert storage.detect_attachment_format(buffer.getvalue()) is None


def test_ooxml_package_without_a_main_part_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/styles.xml", "<styles/>")
        archive.writestr("xl/sharedStrings.xml", "<strings/>")

    assert storage.detect_attachment_format(buffer.getvalue()) is None


def test_ooxml_package_with_both_word_and_excel_main_parts_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<doc/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")

    assert storage.detect_attachment_format(buffer.getvalue()) is None


def test_ooxml_main_part_with_extra_entries_is_still_detected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<doc/>")
        archive.writestr("word/styles.xml", "<styles/>")
        archive.writestr("word/media/image1.png", "")
        archive.writestr("docProps/core.xml", "<core/>")

    detected = storage.detect_attachment_format(buffer.getvalue())

    assert detected is not None
    assert detected.content_type == DOCX_CONTENT_TYPE
    assert detected.extension == ".docx"


def test_sanitize_original_name_strips_paths_and_controls() -> None:
    assert storage.sanitize_original_name("../../etc/passwd") == "passwd"
    assert storage.sanitize_original_name(r"..\..\报价 单.pdf") == "报价 单.pdf"
    assert storage.sanitize_original_name("报价单\r\n.pdf") == "报价单.pdf"
    assert storage.sanitize_original_name("   ") is None
    assert storage.sanitize_original_name("///") is None
    assert storage.sanitize_original_name(None) is None
    assert storage.sanitize_original_name("x" * 400) == "x" * 255


def test_resolve_attachment_path_rejects_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))
    root.mkdir()

    inside = root / "contact-follow-up" / "1" / "a.pdf"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(PDF)

    assert storage.resolve_attachment_path("contact-follow-up/1/a.pdf") == inside
    assert storage.resolve_attachment_path("contact-follow-up/1/missing.pdf") is None
    assert storage.resolve_attachment_path("../media/a.pdf") is None
    assert storage.resolve_attachment_path("/etc/passwd") is None
    assert storage.resolve_attachment_path("contact-follow-up/../../x") is None


def test_resolve_attachment_path_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF)
    link = root / "contact-follow-up" / "1" / "a.pdf"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    assert storage.resolve_attachment_path("contact-follow-up/1/a.pdf") is None


async def test_store_attachment_writes_random_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))

    stored = await storage.store_attachment(
        follow_up_id=7,
        content_type="application/pdf; charset=binary",
        chunks=chunks(PDF),
    )
    path = storage.resolve_attachment_path(stored.storage_path)

    assert path is not None
    assert stored.storage_path.startswith("contact-follow-up/7/")
    assert not stored.storage_path.startswith("/")
    assert path.parent == root / "contact-follow-up" / "7"
    assert path.read_bytes() == PDF
    assert stored.content_type == "application/pdf"
    assert stored.size_bytes == len(PDF)
    assert path.name.endswith(".pdf")
    assert path.name != "document.pdf"
    assert UUID(path.stem)


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (JPEG, "image/jpg"),
        (JPEG, "image/jpeg; charset=binary"),
        (PNG, None),
        (XLSX, XLSX_CONTENT_TYPE),
    ],
)
async def test_store_attachment_accepts_supported_declared_types(
    content: bytes,
    content_type: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "a"))

    stored = await storage.store_attachment(
        follow_up_id=1,
        content_type=content_type,
        chunks=chunks(content),
    )

    assert stored.size_bytes == len(content)


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (PDF, "image/png"),
        (PDF, "text/plain"),
        (b"not-an-attachment", None),
        (b"not-an-attachment", "application/pdf"),
    ],
)
async def test_store_attachment_rejects_unsupported_or_mismatched_content(
    content: bytes,
    content_type: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))

    with pytest.raises(BusinessError) as error:
        await storage.store_attachment(
            follow_up_id=1,
            content_type=content_type,
            chunks=chunks(content),
        )

    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID
    assert error.value.http_status == 422
    assert not root.exists() or not any(root.rglob("*"))


async def test_store_attachment_enforces_call_time_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))
    monkeypatch.setattr(settings, "follow_up_attachment_max_bytes", 10)

    with pytest.raises(BusinessError, match="10-byte limit") as error:
        await storage.store_attachment(
            follow_up_id=1,
            content_type="application/pdf",
            chunks=chunks(PDF),
        )

    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID
    assert error.value.http_status == 422
    assert not root.exists() or not any(root.rglob("*"))


async def test_store_attachment_accepts_exact_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "a"))
    monkeypatch.setattr(settings, "follow_up_attachment_max_bytes", len(PDF))

    stored = await storage.store_attachment(
        follow_up_id=1,
        content_type=None,
        chunks=chunks(PDF),
    )

    assert stored.size_bytes == len(PDF)


async def test_store_attachment_runs_filesystem_write_in_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(tmp_path / "a"))
    main_thread = threading.current_thread()
    write_threads: list[threading.Thread] = []
    original_write_atomic = storage._write_atomic

    def record_write_thread(directory: Path, filename: str, content: bytes) -> None:
        write_threads.append(threading.current_thread())
        original_write_atomic(directory, filename, content)

    monkeypatch.setattr(storage, "_write_atomic", record_write_thread)

    await storage.store_attachment(
        follow_up_id=1,
        content_type=None,
        chunks=chunks(PDF),
    )

    assert len(write_threads) == 1
    assert write_threads[0] is not main_thread


async def test_store_attachment_rejects_symlinked_managed_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "contact-follow-up").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BusinessError) as error:
        await storage.store_attachment(
            follow_up_id=1,
            content_type=None,
            chunks=chunks(PDF),
        )

    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID
    assert error.value.http_status == 422
    assert list(outside.rglob("*")) == []


async def test_remove_attachment_file_only_accepts_safe_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "follow_up_attachment_root", str(root))
    managed = root / "contact-follow-up" / "1" / "a.pdf"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(PDF)

    assert await storage.remove_attachment_file("contact-follow-up/1/a.pdf") is True
    assert await storage.remove_attachment_file("contact-follow-up/1/a.pdf") is False
    assert await storage.remove_attachment_file("../outside.pdf") is False
    assert await storage.remove_attachment_file("/etc/passwd") is False
    assert managed.exists() is False
