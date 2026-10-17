"""Colleague directory service tests."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.models.department import Department, DepartmentStatus
from app.models.user import User, UserStatus
from app.models.user_profile import UserProfile
from app.services import directory as directory_service

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


async def _make_department(
    session: AsyncSession,
    *,
    name: str,
    sort_order: int = 0,
    status: DepartmentStatus = DepartmentStatus.ACTIVE,
    is_system: bool = False,
    deleted_at: datetime | None = None,
) -> Department:
    department = Department(
        name=name,
        path=f"/directory-{uuid4().hex}",
        sort_order=sort_order,
        status=status,
        is_system=is_system,
        deleted_at=deleted_at,
    )
    session.add(department)
    await session.flush()
    return department


async def _make_user(
    session: AsyncSession,
    *,
    username: str,
    nickname: str | None,
    department_id: int | None = None,
    status: UserStatus = UserStatus.ACTIVE,
    deleted_at: datetime | None = None,
    avatar: str | None = None,
) -> User:
    user = User(
        feishu_open_id=f"ou_{uuid4().hex}",
        username=username,
        nickname=nickname,
        avatar=avatar,
        department_id=department_id,
        status=status,
        deleted_at=deleted_at,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_profile(
    session: AsyncSession,
    *,
    user_id: int,
    signature: str | None = None,
    bio: str | None = None,
    work_phone: str | None = None,
    extension: str | None = None,
    work_email: str | None = None,
    office_location: str | None = None,
    seat: str | None = None,
    expertise_tags: list[str] | None = None,
    personal_links: list[dict[str, str]] | None = None,
    hidden_public_fields: list[str] | None = None,
) -> UserProfile:
    profile = UserProfile(
        user_id=user_id,
        signature=signature,
        bio=bio,
        work_phone=work_phone,
        extension=extension,
        work_email=work_email,
        office_location=office_location,
        seat=seat,
        expertise_tags=expertise_tags if expertise_tags is not None else [],
        personal_links=personal_links if personal_links is not None else [],
        hidden_public_fields=(
            hidden_public_fields if hidden_public_fields is not None else []
        ),
    )
    session.add(profile)
    await session.flush()
    return profile


async def test_list_only_returns_active_non_deleted_users(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="在职状态部")
    active_user = await _make_user(
        session,
        username=f"active-{uuid4().hex}",
        nickname="Active",
        department_id=department.id,
    )
    await _make_user(
        session,
        username=f"disabled-{uuid4().hex}",
        nickname="Disabled",
        department_id=department.id,
        status=UserStatus.DISABLED,
    )
    await _make_user(
        session,
        username=f"deleted-{uuid4().hex}",
        nickname="Deleted",
        department_id=department.id,
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )

    page = await directory_service.list_directory(
        session,
        page=1,
        page_size=100,
        keyword=None,
        department_id=department.id,
    )

    assert page.total == 1
    assert [item.id for item in page.items] == [active_user.id]


async def test_keyword_matches_nickname_and_username(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="关键词部")
    token = uuid4().hex[:12]
    nickname_match = await _make_user(
        session,
        username=f"nickname-other-{uuid4().hex}",
        nickname=f"Nickname {token}",
        department_id=department.id,
    )
    username_match = await _make_user(
        session,
        username=f"keyword-{token}",
        nickname="Username Match",
        department_id=department.id,
    )

    page = await directory_service.list_directory(
        session,
        page=1,
        page_size=100,
        keyword=token,
        department_id=department.id,
    )

    assert page.total == 2
    assert {item.id for item in page.items} == {
        nickname_match.id,
        username_match.id,
    }


async def test_department_filter_is_exact_not_subtree(
    session: AsyncSession,
) -> None:
    parent = await _make_department(session, name="父部门")
    child = await _make_department(session, name="子部门")
    child.parent_id = parent.id
    parent_user = await _make_user(
        session,
        username=f"parent-{uuid4().hex}",
        nickname="Parent User",
        department_id=parent.id,
    )
    await _make_user(
        session,
        username=f"child-{uuid4().hex}",
        nickname="Child User",
        department_id=child.id,
    )

    page = await directory_service.list_directory(
        session,
        page=1,
        page_size=100,
        keyword=None,
        department_id=parent.id,
    )

    assert page.total == 1
    assert [item.id for item in page.items] == [parent_user.id]


def test_display_name_falls_back_from_nickname_to_username_to_id() -> None:
    assert directory_service.display_name("Nickname", "username", 1) == "Nickname"
    assert directory_service.display_name(None, "username", 2) == "username"
    assert directory_service.display_name(None, None, 3) == "用户3"


async def test_sort_order_display_name_then_departmentless_last(
    session: AsyncSession,
) -> None:
    token = uuid4().hex[:12]
    later_department = await _make_department(
        session,
        name="后排序部",
        sort_order=20,
    )
    earlier_department = await _make_department(
        session,
        name="先排序部",
        sort_order=10,
    )
    beta = await _make_user(
        session,
        username=f"sort-{token}-beta",
        nickname="Beta",
        department_id=earlier_department.id,
    )
    alpha = await _make_user(
        session,
        username=f"sort-{token}-alpha",
        nickname="Alpha",
        department_id=earlier_department.id,
    )
    zulu = await _make_user(
        session,
        username=f"sort-{token}-zulu",
        nickname="Zulu",
        department_id=later_department.id,
    )
    departmentless = await _make_user(
        session,
        username=f"sort-{token}-none",
        nickname="Aardvark",
    )

    page = await directory_service.list_directory(
        session,
        page=1,
        page_size=100,
        keyword=token,
        department_id=None,
    )

    assert [item.id for item in page.items] == [
        alpha.id,
        beta.id,
        zulu.id,
        departmentless.id,
    ]


async def test_soft_deleted_department_still_renders_name(
    session: AsyncSession,
) -> None:
    token = uuid4().hex[:12]
    deleted_department = await _make_department(
        session,
        name="已删除部门",
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )
    with_deleted_department = await _make_user(
        session,
        username=f"deleted-department-{token}",
        nickname="With department",
        department_id=deleted_department.id,
    )
    departmentless = await _make_user(
        session,
        username=f"departmentless-{token}",
        nickname="Without department",
    )

    page = await directory_service.list_directory(
        session,
        page=1,
        page_size=100,
        keyword=token,
        department_id=None,
    )

    by_id = {item.id: item for item in page.items}
    assert by_id[with_deleted_department.id].department_name == "已删除部门"
    assert by_id[departmentless.id].department_name is None


async def test_hidden_public_fields_clip_avatar_and_signature(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="隐私部")
    user = await _make_user(
        session,
        username=f"private-{uuid4().hex}",
        nickname="Private User",
        department_id=department.id,
        avatar="/media/avatars/private.png",
    )
    session.add(
        UserProfile(
            user_id=user.id,
            signature="Hidden signature",
            hidden_public_fields=["avatar", "signature"],
        )
    )
    await session.flush()

    page = await directory_service.list_directory(
        session,
        page=1,
        page_size=100,
        keyword=None,
        department_id=department.id,
    )

    assert page.total == 1
    assert page.items[0].avatar is None
    assert page.items[0].signature is None


def test_clip_list_only_hides_when_key_is_hidden() -> None:
    values = ["one"]

    assert directory_service.clip_list(values, key="skills", hidden=[]) == values
    assert directory_service.clip_list([], key="skills", hidden=[]) == []
    assert directory_service.clip_list(values, key="skills", hidden=["skills"]) is None


async def test_pagination_with_identical_sort_keys_is_disjoint_and_complete(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="稳定分页部", sort_order=1)
    users = []
    for _ in range(5):
        users.append(
            await _make_user(
                session,
                username=f"pagination-{uuid4().hex}",
                nickname="Same Name",
                department_id=department.id,
            )
        )

    pages = [
        await directory_service.list_directory(
            session,
            page=page,
            page_size=2,
            keyword=None,
            department_id=department.id,
        )
        for page in (1, 2, 3)
    ]

    collected_ids = [
        item.id
        for page in pages
        for item in page.items
    ]
    assert [page.total for page in pages] == [5, 5, 5]
    assert [len(page.items) for page in pages] == [2, 2, 1]
    assert len(set(collected_ids)) == 5
    assert set(collected_ids) == {user.id for user in users}
    assert collected_ids == sorted(user.id for user in users)


async def test_directory_profile_returns_business_card_fields(
    session: AsyncSession,
) -> None:
    department = await _make_department(
        session,
        name="已删除通讯录部",
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )
    user = await _make_user(
        session,
        username=f"card-{uuid4().hex}",
        nickname="Card User",
        department_id=department.id,
        avatar="/media/avatars/card.png",
    )
    await _make_profile(
        session,
        user_id=user.id,
        signature="Signature",
        bio="Biography",
        work_phone="+8613800138000",
        extension="1001",
        work_email="card@example.com",
        office_location="Shanghai",
        seat="A-12",
        expertise_tags=["Python"],
        personal_links=[
            {"label": "Profile", "url": "https://example.com/profile"},
        ],
    )

    result = await directory_service.get_directory_profile(
        session,
        user_id=user.id,
    )

    assert result.id == user.id
    assert result.display_name == "Card User"
    assert result.nickname == "Card User"
    assert result.department_name == "已删除通讯录部"
    assert result.status == UserStatus.ACTIVE
    assert result.avatar == "/media/avatars/card.png"
    assert result.signature == "Signature"
    assert result.bio == "Biography"
    assert result.work_phone == "+8613800138000"
    assert result.extension == "1001"
    assert result.work_email == "card@example.com"
    assert result.office_location == "Shanghai"
    assert result.seat == "A-12"
    assert result.expertise_tags == ["Python"]
    assert result.personal_links is not None
    assert result.personal_links[0].label == "Profile"
    assert result.personal_links[0].url == "https://example.com/profile"


async def test_directory_profile_clips_hidden_scalars_and_preserves_empty_lists(
    session: AsyncSession,
) -> None:
    user = await _make_user(
        session,
        username=f"clipped-{uuid4().hex}",
        nickname="Clipped User",
    )
    await _make_profile(
        session,
        user_id=user.id,
        bio="Private biography",
        seat="B-21",
        expertise_tags=[],
        personal_links=[],
        hidden_public_fields=["bio"],
    )

    result = await directory_service.get_directory_profile(
        session,
        user_id=user.id,
    )

    assert result.bio is None
    assert result.seat == "B-21"
    assert result.expertise_tags == []
    assert result.personal_links == []


async def test_directory_profile_excludes_disabled_and_deleted_users(
    session: AsyncSession,
) -> None:
    disabled = await _make_user(
        session,
        username=f"disabled-card-{uuid4().hex}",
        nickname="Disabled",
        status=UserStatus.DISABLED,
    )
    deleted = await _make_user(
        session,
        username=f"deleted-card-{uuid4().hex}",
        nickname="Deleted",
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )

    for user_id in (disabled.id, deleted.id):
        with pytest.raises(BusinessError) as error:
            await directory_service.get_directory_profile(
                session,
                user_id=user_id,
            )
        assert error.value.code == ErrorCode.USER_NOT_FOUND
        assert error.value.http_status == 404
