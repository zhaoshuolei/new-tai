import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.repositories import department as department_repository
from app.schemas.department import DepartmentCreate, DepartmentUpdate
from app.services.department import (
    create_department,
    delete_department,
    get_tree,
    list_subtree_ids,
    update_department,
)

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        test_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()
    await engine.dispose()


async def add_system_department(
    session: AsyncSession,
    *,
    name: str = "待分配",
) -> Department:
    department = Department(
        name=name,
        path="",
        sort_order=0,
        status=DepartmentStatus.ACTIVE,
        is_system=True,
    )
    session.add(department)
    await session.flush()
    department.path = f"/{department.id}/"
    await session.commit()
    return department


async def test_create_root_department_sets_path(session: AsyncSession) -> None:
    department = await create_department(
        session,
        DepartmentCreate(name="总部"),
    )

    assert department.path == f"/{department.id}/"


async def test_create_child_department_appends_parent_path(
    session: AsyncSession,
) -> None:
    parent = await create_department(
        session,
        DepartmentCreate(name="总部"),
    )

    child = await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=parent.id),
    )

    assert child.path == f"{parent.path}{child.id}/"


async def test_get_tree_nests_children(session: AsyncSession) -> None:
    root = await create_department(session, DepartmentCreate(name="总部"))
    child = await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=root.id),
    )
    other_root = await create_department(session, DepartmentCreate(name="财务部"))

    tree = await get_tree(session)

    assert [node.id for node in tree] == [root.id, other_root.id]
    root_node = next(node for node in tree if node.id == root.id)
    assert [node.id for node in root_node.children] == [child.id]


async def test_delete_department_with_children_returns_5001(
    session: AsyncSession,
) -> None:
    parent = await create_department(session, DepartmentCreate(name="总部"))
    await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=parent.id),
    )

    with pytest.raises(BusinessError) as error:
        await delete_department(session, parent.id)

    assert error.value.code == ErrorCode.DEPT_HAS_CHILDREN


async def test_delete_department_with_users_returns_5002(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    department = await create_department(session, DepartmentCreate(name="总部"))

    async def has_users(
        _session: AsyncSession,
        department_id: int,
    ) -> bool:
        return department_id == department.id

    monkeypatch.setattr(department_repository, "has_users", has_users)

    with pytest.raises(BusinessError) as error:
        await delete_department(session, department.id)

    assert error.value.code == ErrorCode.DEPT_HAS_USERS


async def test_delete_department_without_users_succeeds(
    session: AsyncSession,
) -> None:
    department = await create_department(session, DepartmentCreate(name="总部"))

    await delete_department(session, department.id)

    assert await department_repository.get_by_id(session, department.id) is None


async def test_system_department_cannot_be_renamed_or_deleted(
    session: AsyncSession,
) -> None:
    department = await add_system_department(session)

    with pytest.raises(BusinessError) as update_error:
        await update_department(
            session,
            department.id,
            DepartmentUpdate(name="新名称"),
        )

    with pytest.raises(BusinessError) as delete_error:
        await delete_department(session, department.id)

    assert update_error.value.code == ErrorCode.SYSTEM_DEPT_IMMUTABLE
    assert delete_error.value.code == ErrorCode.SYSTEM_DEPT_IMMUTABLE


async def test_create_child_under_disabled_parent_is_rejected(
    session: AsyncSession,
) -> None:
    parent = await create_department(session, DepartmentCreate(name="总部"))
    await update_department(
        session,
        parent.id,
        DepartmentUpdate(status=DepartmentStatus.DISABLED),
    )

    with pytest.raises(BusinessError) as error:
        await create_department(
            session,
            DepartmentCreate(name="研发部", parent_id=parent.id),
        )

    assert error.value.code == ErrorCode.INVALID_PARAMETER


async def test_update_department_changes_mutable_fields(
    session: AsyncSession,
) -> None:
    department = await create_department(session, DepartmentCreate(name="总部"))

    updated = await update_department(
        session,
        department.id,
        DepartmentUpdate(
            name="集团总部",
            sort_order=10,
            status=DepartmentStatus.DISABLED,
        ),
    )

    assert updated.name == "集团总部"
    assert updated.sort_order == 10
    assert updated.status == DepartmentStatus.DISABLED


async def test_list_subtree_ids_includes_self_and_all_descendants(
    session: AsyncSession,
) -> None:
    root = await create_department(session, DepartmentCreate(name="总部"))
    child = await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=root.id),
    )
    grandchild = await create_department(
        session,
        DepartmentCreate(name="平台组", parent_id=child.id),
    )
    await create_department(session, DepartmentCreate(name="财务部"))

    ids = await list_subtree_ids(session, root.id)

    assert set(ids) == {root.id, child.id, grandchild.id}


async def test_get_tree_filters_disabled_departments_by_default(
    session: AsyncSession,
) -> None:
    active = await create_department(session, DepartmentCreate(name="总部"))
    disabled = await create_department(session, DepartmentCreate(name="旧部门"))
    await update_department(
        session,
        disabled.id,
        DepartmentUpdate(status=DepartmentStatus.DISABLED),
    )

    visible_tree = await get_tree(session)
    full_tree = await get_tree(session, include_disabled=True)

    assert [node.id for node in visible_tree] == [active.id]
    assert {node.id for node in full_tree} == {active.id, disabled.id}


async def test_get_tree_attaches_child_to_nearest_visible_ancestor(
    session: AsyncSession,
) -> None:
    root = await create_department(session, DepartmentCreate(name="总部"))
    hidden = await create_department(
        session,
        DepartmentCreate(name="停用中间层", parent_id=root.id),
    )
    child = await create_department(
        session,
        DepartmentCreate(name="活跃子部门", parent_id=hidden.id),
    )
    await update_department(
        session,
        hidden.id,
        DepartmentUpdate(status=DepartmentStatus.DISABLED),
    )

    tree = await get_tree(session)

    assert [node.id for node in tree] == [root.id]
    assert [node.id for node in tree[0].children] == [child.id]


async def test_department_endpoints_return_success_envelope(
    session: AsyncSession,
) -> None:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            create_response = await client.post(
                "/api/v1/departments",
                json={"name": "总部"},
            )
            tree_response = await client.get("/api/v1/departments/tree")
            health_response = await client.get("/health/live")
    finally:
        application.dependency_overrides.clear()

    assert create_response.status_code == 201
    assert create_response.json()["code"] == 0
    assert create_response.json()["data"]["path"].startswith("/")
    assert tree_response.status_code == 200
    assert tree_response.json()["code"] == 0
    assert tree_response.json()["data"][0]["name"] == "总部"
    assert health_response.status_code == 200


def test_openapi_uses_api_v1_prefix_for_business_routes() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/v1/departments/tree" in paths
    assert "/departments/tree" not in paths
    assert "/health/live" in paths
    assert "/health/ready" in paths
