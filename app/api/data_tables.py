"""可配置数据表定义接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_meta import register_audit_route
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.core.permissions import require_permission
from app.core.response import SuccessEnvelope, success
from app.db.session import get_session
from app.schemas.data_query import DataRecordSearch, DataSearchResult
from app.schemas.data_table import (
    DataTableCreate,
    DataTableDefinitionRead,
    DataTableListItem,
    DataTableReplace,
)
from app.services import data_query as data_query_service
from app.services import data_table as data_table_service

router = APIRouter(prefix="/data-tables", tags=["数据管理"])


@router.get(
    "",
    response_model=SuccessEnvelope[list[DataTableListItem]],
    dependencies=[Depends(require_permission("data:read"))],
)
async def list_data_tables(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    definitions = await data_table_service.list_tables(session, context=context)
    summaries = await data_query_service.list_table_visibility_summaries(
        session,
        [definition.table.id for definition in definitions],
        context=context,
    )
    return success(
        [
            DataTableListItem.from_resolved_with_visibility(
                definition,
                summaries[definition.table.id],
            )
            for definition in definitions
        ]
    )


@router.post(
    "",
    response_model=SuccessEnvelope[DataTableDefinitionRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("data:create"))],
)
async def create_data_table(
    payload: DataTableCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    definition = await data_table_service.create_table(
        session,
        payload,
        context=context,
    )
    return success(
        DataTableDefinitionRead.from_resolved(definition),
        message="created",
    )


@router.post(
    "/{table_id}/records/search",
    response_model=SuccessEnvelope[DataSearchResult],
    dependencies=[Depends(require_permission("data:read"))],
)
async def search_data_records(
    table_id: int,
    payload: DataRecordSearch,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    result = await data_query_service.search_records(
        session,
        table_id,
        payload,
        context=context,
    )
    return success(result)


@router.get(
    "/{table_id}",
    response_model=SuccessEnvelope[DataTableDefinitionRead],
    dependencies=[Depends(require_permission("data:read"))],
)
async def get_data_table(
    table_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    definition = await data_table_service.get_table(
        session,
        table_id,
        context=context,
    )
    return success(DataTableDefinitionRead.from_resolved(definition))


@router.put(
    "/{table_id}",
    response_model=SuccessEnvelope[DataTableDefinitionRead],
    dependencies=[Depends(require_permission("data:update"))],
)
async def replace_data_table(
    table_id: int,
    payload: DataTableReplace,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    definition = await data_table_service.replace_table(
        session,
        table_id,
        payload,
        context=context,
    )
    return success(DataTableDefinitionRead.from_resolved(definition))


@router.delete(
    "/{table_id}",
    response_model=SuccessEnvelope[None],
    dependencies=[Depends(require_permission("data:delete"))],
)
async def delete_data_table(
    table_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(get_current_context)],
) -> dict[str, Any]:
    await data_table_service.delete_table(
        session,
        table_id,
        context=context,
    )
    return success(message="deleted")


register_audit_route(
    "POST",
    "/data-tables",
    action="data_table:create",
    resource_type="custom_data_table",
)
register_audit_route(
    "PUT",
    "/data-tables/{table_id}",
    action="data_table:update",
    resource_type="custom_data_table",
    resource_id_param="table_id",
)
register_audit_route(
    "DELETE",
    "/data-tables/{table_id}",
    action="data_table:delete",
    resource_type="custom_data_table",
    resource_id_param="table_id",
)


__all__: tuple[str, ...] = (
    "create_data_table",
    "delete_data_table",
    "get_data_table",
    "list_data_tables",
    "replace_data_table",
    "router",
    "search_data_records",
)
