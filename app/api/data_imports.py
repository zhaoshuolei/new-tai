"""Custom data import request, status, diagnostics, source, and template APIs."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any, BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.config import settings
from app.core.deps import get_current_context
from app.core.errors import BusinessError, ErrorCode
from app.core.middleware import ReceiveBodyLimitRule
from app.core.permissions import require_permission
from app.core.response import MAX_PAGE_SIZE, Page, SuccessEnvelope, success
from app.db.session import get_session
from app.models.data_management import (
    DataImportError,
    DataImportJob,
    DataImportSource,
    DataTableDefinition,
)
from app.repositories import data_import as data_import_repository
from app.schemas.data_import import (
    DataImportErrorRead,
    DataImportRead,
    parse_data_import_json_request,
)
from app.services import data_import as data_import_service
from app.services import data_table as data_table_service
from app.services.data_import_parser import build_excel_template
from app.services.data_import_storage import (
    StoredImportSource,
    delete_import_source,
    read_import_source,
    save_import_bytes,
)
from app.services.private_storage import sanitize_original_name

router = APIRouter(prefix="/data-tables", tags=["数据管理"])
logger = logging.getLogger(__name__)

_HARD_MAX_IMPORT_BYTES = 50 * 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_READ_CHUNK_SIZE = 64 * 1024
_EXCEL_IMPORT_PATH = re.compile(r"^/api/v1/data-tables/[0-9]+/imports/excel$")
_JSON_IMPORT_PATH = re.compile(r"^/api/v1/data-tables/[0-9]+/imports/json$")
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SOURCE_CONTENT_TYPES = {
    DataImportSource.EXCEL: _XLSX_CONTENT_TYPE,
    DataImportSource.API: "application/json",
}


def _import_limit() -> int:
    return min(settings.data_import_max_bytes, _HARD_MAX_IMPORT_BYTES)


def _invalid_import(message: str) -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_INVALID,
        message,
        http_status=422,
    )


def _invalid_parameters() -> BusinessError:
    return BusinessError(
        ErrorCode.INVALID_PARAMETER,
        "Invalid request parameters",
        http_status=422,
    )


def _import_not_found() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_IMPORT_NOT_FOUND,
        "Import job not found",
        http_status=404,
    )


def _table_not_found() -> BusinessError:
    return BusinessError(
        ErrorCode.DATA_TABLE_NOT_FOUND,
        "Data table not found",
        http_status=404,
    )


def _oversized_error() -> BusinessError:
    return _invalid_import(
        f"Data import source exceeds the {_import_limit()}-byte limit",
    )


def import_receive_body_limit_rules() -> tuple[ReceiveBodyLimitRule, ...]:
    """Return receive-boundary limits for the two import POST routes."""
    return (
        ReceiveBodyLimitRule(
            path_pattern=_EXCEL_IMPORT_PATH,
            method="POST",
            max_body_bytes=_import_limit,
            multipart_overhead_bytes=_MULTIPART_OVERHEAD_BYTES,
            error_factory=_oversized_error,
        ),
        ReceiveBodyLimitRule(
            path_pattern=_JSON_IMPORT_PATH,
            method="POST",
            max_body_bytes=_import_limit,
            multipart_overhead_bytes=0,
            error_factory=_oversized_error,
        ),
    )


async def _read_bounded_body(request: Request) -> bytes:
    """Read original JSON request bytes without unbounded framework buffering."""
    limit = _import_limit()
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError:
            raise _invalid_import("Import Content-Length is invalid") from None
        if length < 0:
            raise _invalid_import("Import Content-Length is invalid")
        if length > limit:
            raise _oversized_error()

    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > limit:
            raise _oversized_error()
        content.extend(chunk)
    return bytes(content)


async def _read_bounded_upload(upload: UploadFile) -> bytes:
    limit = _import_limit()
    content = bytearray()
    try:
        while chunk := await upload.read(_READ_CHUNK_SIZE):
            if len(content) + len(chunk) > limit:
                raise _oversized_error()
            content.extend(chunk)
    finally:
        await upload.close()
    return bytes(content)


async def _delete_saved_source_best_effort(storage_path: str) -> None:
    try:
        await asyncio.to_thread(delete_import_source, storage_path)
    except Exception:
        # Do not include the private path or original filename in the log.
        logger.warning("Failed to remove an unsaved data import source")


async def _enqueue_after_save(
    session: AsyncSession,
    table_id: int,
    *,
    source: DataImportSource,
    stored: StoredImportSource,
    context: AuthContext,
) -> DataImportJob:
    try:
        job = await data_import_service.enqueue_import(
            session,
            table_id,
            source=source,
            stored=stored,
            context=context,
        )
    except Exception:
        await _delete_saved_source_best_effort(stored.storage_path)
        raise

    # Server-default timestamps are needed by the public status schema.
    await session.refresh(job)
    return job


async def _require_table(session: AsyncSession, table_id: int) -> None:
    exists = await session.scalar(
        select(DataTableDefinition.id).where(DataTableDefinition.id == table_id)
    )
    if exists is None:
        raise _table_not_found()


async def _get_job_for_table(
    session: AsyncSession,
    table_id: int,
    import_id: int,
) -> DataImportJob:
    await _require_table(session, table_id)
    job = await session.scalar(
        select(DataImportJob).where(
            DataImportJob.id == import_id,
            DataImportJob.table_id == table_id,
        )
    )
    if job is None:
        raise _import_not_found()
    return job


def _source_filename(job: DataImportJob) -> str:
    sanitized = sanitize_original_name(job.original_filename)
    if sanitized is not None:
        return sanitized
    suffix = ".xlsx" if job.source == DataImportSource.EXCEL else ".json"
    return f"import-{job.id}{suffix}"


def _content_disposition(filename: str) -> str:
    encoded = quote(filename)
    if encoded != filename:
        return f"attachment; filename*=utf-8''{encoded}"
    return f'attachment; filename="{filename}"'


async def _source_chunks(source: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while chunk := await asyncio.to_thread(source.read, _READ_CHUNK_SIZE):
            yield chunk
    finally:
        await asyncio.to_thread(source.close)


@router.post(
    "/{table_id}/imports/excel",
    response_model=SuccessEnvelope[DataImportRead],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("data:import"))],
)
async def import_excel(
    table_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    form = await request.form()
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise _invalid_parameters()

        original_name = file.filename
        if not (original_name or "").lower().endswith(".xlsx"):
            raise _invalid_import("Excel import file must use the .xlsx suffix")

        content = await _read_bounded_upload(file)
        stored = await asyncio.to_thread(
            save_import_bytes,
            content,
            suffix=".xlsx",
            original_name=original_name,
        )
        job = await _enqueue_after_save(
            session,
            table_id,
            source=DataImportSource.EXCEL,
            stored=stored,
            context=context,
        )
        return success(DataImportRead.model_validate(job), message="queued")
    finally:
        await form.close()


@router.post(
    "/{table_id}/imports/json",
    response_model=SuccessEnvelope[DataImportRead],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("data:import"))],
)
async def import_json(
    table_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    raw_bytes = await _read_bounded_body(request)
    parse_data_import_json_request(raw_bytes)

    stored = await asyncio.to_thread(
        save_import_bytes,
        raw_bytes,
        suffix=".json",
        original_name="data.json",
    )
    job = await _enqueue_after_save(
        session,
        table_id,
        source=DataImportSource.API,
        stored=stored,
        context=context,
    )
    return success(DataImportRead.model_validate(job), message="queued")


@router.get(
    "/{table_id}/imports",
    response_model=SuccessEnvelope[Page[DataImportRead]],
    dependencies=[Depends(require_permission("data:import"))],
)
async def list_imports(
    table_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    await _require_table(session, table_id)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(DataImportJob)
            .where(DataImportJob.table_id == table_id)
        )
        or 0
    )
    offset = (page - 1) * page_size
    jobs = (
        []
        if offset >= total
        else await data_import_repository.list_import_jobs(
            session,
            table_id,
            offset=offset,
            limit=page_size,
        )
    )
    return success(
        Page[DataImportRead](
            items=[DataImportRead.model_validate(job) for job in jobs],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/{table_id}/imports/{import_id}",
    response_model=SuccessEnvelope[DataImportRead],
    dependencies=[Depends(require_permission("data:import"))],
)
async def get_import(
    table_id: int,
    import_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    job = await _get_job_for_table(session, table_id, import_id)
    return success(DataImportRead.model_validate(job))


@router.get(
    "/{table_id}/imports/{import_id}/errors",
    response_model=SuccessEnvelope[Page[DataImportErrorRead]],
    dependencies=[Depends(require_permission("data:import"))],
)
async def list_import_errors(
    table_id: int,
    import_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> dict[str, Any]:
    await _get_job_for_table(session, table_id, import_id)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(DataImportError)
            .where(DataImportError.import_id == import_id)
        )
        or 0
    )
    offset = (page - 1) * page_size
    errors = (
        []
        if offset >= total
        else await data_import_repository.list_import_errors(
            session,
            import_id,
            offset=offset,
            limit=page_size,
        )
    )
    return success(
        Page[DataImportErrorRead](
            items=[DataImportErrorRead.model_validate(error) for error in errors],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/{table_id}/imports/{import_id}/source",
    response_class=StreamingResponse,
    dependencies=[Depends(require_permission("data:import"))],
)
async def download_import_source(
    table_id: int,
    import_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    job = await _get_job_for_table(session, table_id, import_id)
    try:
        source = await asyncio.to_thread(read_import_source, job.storage_path)
    except BusinessError as error:
        if error.code == ErrorCode.DATA_IMPORT_INVALID:
            raise _import_not_found() from None
        raise
    filename = _source_filename(job)
    return StreamingResponse(
        _source_chunks(source),
        media_type=_SOURCE_CONTENT_TYPES[job.source],
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": _content_disposition(filename),
            "Content-Length": str(job.size_bytes),
        },
    )


@router.get(
    "/{table_id}/template.xlsx",
    response_class=Response,
    dependencies=[Depends(require_permission("data:read"))],
)
async def download_template(
    table_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> Response:
    definition = await data_table_service.get_table(
        session,
        table_id,
        context=context,
    )
    content = await asyncio.to_thread(build_excel_template, definition)
    return Response(
        content=content,
        media_type=_XLSX_CONTENT_TYPE,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="template.xlsx"',
        },
    )


register_audit_route(
    "POST",
    "/data-tables/{table_id}/imports/excel",
    action="data_import:request",
    resource_type="custom_data_import",
    resource_id_param="table_id",
)
register_audit_route(
    "POST",
    "/data-tables/{table_id}/imports/json",
    action="data_import:request",
    resource_type="custom_data_import",
    resource_id_param="table_id",
)


__all__: tuple[str, ...] = (
    "download_import_source",
    "download_template",
    "get_import",
    "import_excel",
    "import_json",
    "import_receive_body_limit_rules",
    "list_import_errors",
    "list_imports",
    "router",
)
