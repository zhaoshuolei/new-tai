from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.models.menu import MenuType
from app.repositories import menu as menu_repository
from app.schemas.menu import MenuCreate, MenuUpdate
from app.services.menu import create_menu, delete_menu, update_menu

TEST_DATABASE_URL = (
    "mysql+asyncmy://tianxiadmin:tianxiadmin@127.0.0.1:3306/tianxiadmin_test"
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


class _RecordingResult:
    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    statement: Any = None

    async def scalars(self, statement: Any) -> _RecordingResult:
        self.statement = statement
        return _RecordingResult()


@pytest.mark.asyncio
async def test_lock_menu_nodes_uses_one_ascending_for_update_query() -> None:
    recording = _RecordingSession()

    await menu_repository.lock_menu_nodes(
        cast(AsyncSession, recording),
        {30, 10, 20},
    )

    compiled = str(
        recording.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "IN (10, 20, 30)" in compiled
    assert "ORDER BY menu.id" in compiled
    assert "FOR UPDATE" in compiled


@pytest.mark.asyncio
async def test_list_children_for_update_uses_ascending_locking_query() -> None:
    recording = _RecordingSession()

    await menu_repository.list_children_for_update(
        cast(AsyncSession, recording),
        10,
    )

    compiled = str(
        recording.statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "menu.parent_id = 10" in compiled
    assert "ORDER BY menu.id" in compiled
    assert "FOR UPDATE" in compiled


async def test_update_locks_target_before_locking_children(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="根目录"),
    )
    await create_menu(
        session,
        MenuCreate(
            parent_id=root.id,
            type=MenuType.MENU,
            name="用户管理",
        ),
    )
    original_lock = menu_repository.lock_menu_nodes
    original_children_lock = menu_repository.list_children_for_update
    lock_events: list[tuple[str, object]] = []

    async def record_node_lock(
        lock_session: AsyncSession,
        menu_ids: set[int],
    ) -> dict[int, Any]:
        lock_events.append(("nodes", set(menu_ids)))
        return await original_lock(lock_session, menu_ids)

    async def record_child_lock(
        lock_session: AsyncSession,
        menu_id: int,
    ) -> list[Any]:
        lock_events.append(("children", menu_id))
        return await original_children_lock(lock_session, menu_id)

    monkeypatch.setattr(menu_repository, "lock_menu_nodes", record_node_lock)
    monkeypatch.setattr(
        menu_repository,
        "list_children_for_update",
        record_child_lock,
    )

    await update_menu(
        session,
        root.id,
        MenuUpdate(name="平台管理"),
    )

    assert lock_events == [
        ("nodes", {root.id}),
        ("children", root.id),
    ]


async def test_update_validates_post_lock_children(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await create_menu(
        session,
        MenuCreate(type=MenuType.MENU, name="用户管理"),
    )
    await create_menu(
        session,
        MenuCreate(
            parent_id=parent.id,
            type=MenuType.BUTTON,
            name="新增用户",
        ),
    )

    async def empty_pre_lock_read(
        _session: AsyncSession,
        _menu_id: int,
    ) -> list[Any]:
        return []

    monkeypatch.setattr(
        menu_repository,
        "list_children",
        empty_pre_lock_read,
    )

    with pytest.raises(BusinessError) as error:
        await update_menu(
            session,
            parent.id,
            MenuUpdate(type=MenuType.DIR),
        )

    assert error.value.code == ErrorCode.INVALID_MENU_HIERARCHY


async def test_create_menu_maps_deadlock_to_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await create_menu(
        session,
        MenuCreate(type=MenuType.DIR, name="根目录"),
    )

    async def deadlock(
        _session: AsyncSession,
        _menu_ids: set[int],
    ) -> dict[int, Any]:
        raise OperationalError(
            "SELECT",
            {},
            Exception(1213, "Deadlock found when trying to get lock"),
        )

    monkeypatch.setattr(menu_repository, "lock_menu_nodes", deadlock)

    with pytest.raises(BusinessError) as error:
        await create_menu(
            session,
            MenuCreate(
                parent_id=parent.id,
                type=MenuType.MENU,
                name="用户管理",
            ),
        )

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
    assert "稍后重试" in error.value.message


async def test_update_menu_maps_deadlock_to_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    menu = await create_menu(
        session,
        MenuCreate(type=MenuType.MENU, name="用户管理"),
    )

    async def deadlock(
        _session: AsyncSession,
        _menu_ids: set[int],
    ) -> dict[int, Any]:
        raise OperationalError(
            "SELECT",
            {},
            Exception(1213, "Deadlock found when trying to get lock"),
        )

    monkeypatch.setattr(menu_repository, "lock_menu_nodes", deadlock)

    with pytest.raises(BusinessError) as error:
        await update_menu(
            session,
            menu.id,
            MenuUpdate(name="用户与权限"),
        )

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409


async def test_delete_menu_maps_deadlock_to_conflict(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    menu = await create_menu(
        session,
        MenuCreate(type=MenuType.MENU, name="用户管理"),
    )

    async def deadlock(
        _session: AsyncSession,
        _menu_ids: set[int],
    ) -> dict[int, Any]:
        raise OperationalError(
            "SELECT",
            {},
            Exception(1213, "Deadlock found when trying to get lock"),
        )

    monkeypatch.setattr(menu_repository, "lock_menu_nodes", deadlock)

    with pytest.raises(BusinessError) as error:
        await delete_menu(session, menu.id)

    assert error.value.code == ErrorCode.CONFLICT
    assert error.value.http_status == 409
