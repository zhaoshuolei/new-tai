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
from app.schemas.department import DepartmentCreate, DepartmentUpdate
from app.services.department import (
    create_department,
    move_department,
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


async def test_move_to_self_returns_5003(session: AsyncSession) -> None:
    department = await create_department(session, DepartmentCreate(name="总部"))

    with pytest.raises(BusinessError) as error:
        await move_department(session, department.id, department.id)

    assert error.value.code == ErrorCode.INVALID_DEPT_MOVE


async def test_move_to_own_descendant_returns_5003(
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

    with pytest.raises(BusinessError) as error:
        await move_department(session, root.id, grandchild.id)

    assert error.value.code == ErrorCode.INVALID_DEPT_MOVE


async def test_move_updates_own_path(session: AsyncSession) -> None:
    root = await create_department(session, DepartmentCreate(name="总部"))
    new_parent = await create_department(session, DepartmentCreate(name="新总部"))
    child = await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=root.id),
    )

    moved = await move_department(session, child.id, new_parent.id)

    assert moved.parent_id == new_parent.id
    assert moved.path == f"{new_parent.path}{child.id}/"


async def test_move_cascades_path_to_all_descendants(
    session: AsyncSession,
) -> None:
    root = await create_department(session, DepartmentCreate(name="A"))
    branch = await create_department(
        session,
        DepartmentCreate(name="B", parent_id=root.id),
    )
    child = await create_department(
        session,
        DepartmentCreate(name="C", parent_id=branch.id),
    )
    grandchild = await create_department(
        session,
        DepartmentCreate(name="D", parent_id=child.id),
    )
    great_grandchild = await create_department(
        session,
        DepartmentCreate(name="E", parent_id=grandchild.id),
    )

    assert child.path == f"{root.path}{branch.id}/{child.id}/"

    await move_department(session, branch.id, None)

    assert branch.path == f"/{branch.id}/"
    assert child.path == f"/{branch.id}/{child.id}/"
    assert grandchild.path == f"/{branch.id}/{child.id}/{grandchild.id}/"
    assert great_grandchild.path == (
        f"/{branch.id}/{child.id}/{grandchild.id}/{great_grandchild.id}/"
    )


async def test_move_system_department_returns_5004(
    session: AsyncSession,
) -> None:
    system_department = await add_system_department(session)
    new_parent = await create_department(session, DepartmentCreate(name="总部"))

    with pytest.raises(BusinessError) as error:
        await move_department(session, system_department.id, new_parent.id)

    assert error.value.code == ErrorCode.SYSTEM_DEPT_IMMUTABLE


async def test_move_to_root_sets_path_from_own_id(
    session: AsyncSession,
) -> None:
    root = await create_department(session, DepartmentCreate(name="总部"))
    child = await create_department(
        session,
        DepartmentCreate(name="研发部", parent_id=root.id),
    )

    moved = await move_department(session, child.id, None)

    assert moved.parent_id is None
    assert moved.path == f"/{child.id}/"


async def test_move_to_disabled_parent_returns_5003(
    session: AsyncSession,
) -> None:
    disabled_parent = await create_department(
        session,
        DepartmentCreate(name="停用部门"),
    )
    department = await create_department(session, DepartmentCreate(name="研发部"))
    await update_department(
        session,
        disabled_parent.id,
        DepartmentUpdate(status=DepartmentStatus.DISABLED),
    )

    with pytest.raises(BusinessError) as error:
        await move_department(session, department.id, disabled_parent.id)

    assert error.value.code == ErrorCode.INVALID_DEPT_MOVE


async def test_move_to_missing_parent_returns_5000(
    session: AsyncSession,
) -> None:
    department = await create_department(session, DepartmentCreate(name="研发部"))

    with pytest.raises(BusinessError) as error:
        await move_department(session, department.id, 999999)

    assert error.value.code == ErrorCode.DEPT_NOT_FOUND


async def test_move_endpoint_returns_success_envelope(
    session: AsyncSession,
) -> None:
    new_parent = await create_department(session, DepartmentCreate(name="总部"))
    department = await create_department(session, DepartmentCreate(name="研发部"))
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.patch(
                f"/api/v1/departments/{department.id}/move",
                json={"new_parent_id": new_parent.id},
            )
    finally:
        application.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert response.json()["data"]["parent_id"] == new_parent.id
    assert response.json()["data"]["path"] == f"{new_parent.path}{department.id}/"
