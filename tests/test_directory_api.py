"""Colleague directory API tests."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.audit_meta import ActorSnapshot
from app.core.auth_context import AuthContext
from app.core.deps import get_current_context
from app.db.session import get_session
from app.main import create_app
from app.models.department import Department, DepartmentStatus
from app.models.user import User, UserStatus
from app.models.user_profile import UserProfile

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


@asynccontextmanager
async def api_client(
    session: AsyncSession,
    context: AuthContext,
) -> AsyncIterator[AsyncClient]:
    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_context(request: Request) -> AuthContext:
        request.state.user_id = context.user_id
        request.state.actor = ActorSnapshot(
            user_id=context.user_id,
            username=context.username,
            nickname=context.nickname,
        )
        return context

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_current_context] = override_context
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        application.dependency_overrides.clear()


def _context(user_id: int) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        role_id=None,
        department_id=None,
        data_scope=None,
        custom_department_ids=frozenset(),
        include_children=False,
        permission_codes=frozenset(),
        is_super_admin=False,
        username=f"context-{user_id}",
        nickname="Directory Reader",
    )


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
        path=f"/directory-api-{uuid4().hex}",
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
    avatar: str | None = None,
    status: UserStatus = UserStatus.ACTIVE,
    deleted_at: datetime | None = None,
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


async def test_directory_list_requires_no_user_read_permission(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="通讯录权限部")
    token = uuid4().hex[:12]
    user = await _make_user(
        session,
        username=f"directory-{token}",
        nickname="Directory User",
        department_id=department.id,
        avatar="/media/avatars/directory.png",
    )

    async with api_client(session, _context(user.id)) as client:
        response = await client.get(
            "/api/v1/directory",
            params={"keyword": token, "page": 1, "page_size": 10},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["total"] == 1
    item = payload["data"]["items"][0]
    assert set(item) == {
        "id",
        "display_name",
        "nickname",
        "avatar",
        "signature",
        "department_name",
    }
    assert item["id"] == user.id
    assert item["department_name"] == "通讯录权限部"


async def test_directory_list_clips_hidden_card_fields(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="通讯录隐私部")
    token = uuid4().hex[:12]
    user = await _make_user(
        session,
        username=f"hidden-{token}",
        nickname="Hidden User",
        department_id=department.id,
        avatar="/media/avatars/hidden.png",
    )
    session.add(
        UserProfile(
            user_id=user.id,
            signature="do not expose",
            hidden_public_fields=["avatar", "signature"],
        )
    )
    await session.flush()

    async with api_client(session, _context(user.id)) as client:
        response = await client.get("/api/v1/directory", params={"keyword": token})

    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["avatar"] is None
    assert item["signature"] is None


async def test_department_filter_options_exclude_system_disabled_and_deleted(
    session: AsyncSession,
) -> None:
    visible = await _make_department(
        session,
        name="可见部门",
        sort_order=-100,
    )
    system = await _make_department(
        session,
        name="待分配",
        sort_order=-110,
        is_system=True,
    )
    disabled = await _make_department(
        session,
        name="停用部门",
        sort_order=-120,
        status=DepartmentStatus.DISABLED,
    )
    deleted = await _make_department(
        session,
        name="已删除部门",
        sort_order=-130,
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )
    visible_tie = await _make_department(
        session,
        name="同序部门",
        sort_order=-100,
    )

    async with api_client(session, _context(visible.id)) as client:
        response = await client.get("/api/v1/directory/departments")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    departments = payload["data"]
    by_id = {department["id"]: department for department in departments}
    assert visible.id in by_id
    assert visible_tie.id in by_id
    assert system.id not in by_id
    assert disabled.id not in by_id
    assert deleted.id not in by_id
    assert departments == sorted(
        departments,
        key=lambda department: (department["sort_order"], department["id"]),
    )
    assert set(by_id[visible.id]) == {"id", "name", "parent_id", "sort_order"}


async def test_directory_profile_returns_exact_business_card_key_set(
    session: AsyncSession,
) -> None:
    department = await _make_department(session, name="名片部")
    user = await _make_user(
        session,
        username=f"business-card-{uuid4().hex}",
        nickname="Business Card",
        department_id=department.id,
        avatar="/media/avatars/business-card.png",
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

    async with api_client(session, _context(999_999_998)) as client:
        response = await client.get(f"/api/v1/directory/{user.id}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    business_card_keys = {
        "avatar",
        "signature",
        "bio",
        "work_phone",
        "extension",
        "work_email",
        "office_location",
        "seat",
        "expertise_tags",
        "personal_links",
    }
    assert (
        set(data)
        == {
            "id",
            "display_name",
            "nickname",
            "department_name",
            "status",
        }
        | business_card_keys
    )
    assert {
        "personal_phone",
        "birthday",
        "emergency_contact_name",
        "emergency_contact_phone",
    }.isdisjoint(data)
    assert data["id"] == user.id
    assert data["display_name"] == "Business Card"
    assert data["nickname"] == "Business Card"
    assert data["department_name"] == "名片部"
    assert data["status"] == "active"
    assert data["avatar"] == "/media/avatars/business-card.png"
    assert data["signature"] == "Signature"
    assert data["bio"] == "Biography"
    assert data["work_phone"] == "+8613800138000"
    assert data["extension"] == "1001"
    assert data["work_email"] == "card@example.com"
    assert data["office_location"] == "Shanghai"
    assert data["seat"] == "A-12"
    assert data["expertise_tags"] == ["Python"]
    assert data["personal_links"] == [
        {"label": "Profile", "url": "https://example.com/profile"},
    ]


async def test_directory_profile_hidden_and_unfilled_bio_are_indistinguishable(
    session: AsyncSession,
) -> None:
    hidden = await _make_user(
        session,
        username=f"hidden-bio-{uuid4().hex}",
        nickname="Hidden Bio",
    )
    unfilled = await _make_user(
        session,
        username=f"unfilled-bio-{uuid4().hex}",
        nickname="Unfilled Bio",
    )
    await _make_profile(
        session,
        user_id=hidden.id,
        bio="Do not expose",
        seat="C-01",
        expertise_tags=[],
        personal_links=[],
        hidden_public_fields=["bio"],
    )
    await _make_profile(
        session,
        user_id=unfilled.id,
        expertise_tags=[],
        personal_links=[],
    )

    async with api_client(session, _context(999_999_997)) as client:
        hidden_response = await client.get(f"/api/v1/directory/{hidden.id}")
        unfilled_response = await client.get(f"/api/v1/directory/{unfilled.id}")

    assert hidden_response.status_code == 200, hidden_response.text
    assert unfilled_response.status_code == 200, unfilled_response.text
    hidden_data = hidden_response.json()["data"]
    unfilled_data = unfilled_response.json()["data"]
    assert "bio" in hidden_data
    assert "bio" in unfilled_data
    assert hidden_data["bio"] == unfilled_data["bio"] is None
    assert hidden_data["seat"] == "C-01"
    assert hidden_data["expertise_tags"] == []
    assert hidden_data["personal_links"] == []


async def test_directory_profile_without_profile_row_returns_empty_lists(
    session: AsyncSession,
) -> None:
    user = await _make_user(
        session,
        username=f"no-profile-{uuid4().hex}",
        nickname="No Profile",
    )

    async with api_client(session, _context(999_999_994)) as client:
        response = await client.get(f"/api/v1/directory/{user.id}")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["expertise_tags"] == []
    assert data["personal_links"] == []


async def test_directory_profile_clips_hidden_list_fields_by_contents(
    session: AsyncSession,
) -> None:
    hidden_tags_with_values = await _make_user(
        session,
        username=f"hidden-tags-values-{uuid4().hex}",
        nickname="Hidden Tags With Values",
    )
    hidden_tags_empty = await _make_user(
        session,
        username=f"hidden-tags-empty-{uuid4().hex}",
        nickname="Hidden Tags Empty",
    )
    hidden_links_with_values = await _make_user(
        session,
        username=f"hidden-links-values-{uuid4().hex}",
        nickname="Hidden Links With Values",
    )
    hidden_links_empty = await _make_user(
        session,
        username=f"hidden-links-empty-{uuid4().hex}",
        nickname="Hidden Links Empty",
    )
    await _make_profile(
        session,
        user_id=hidden_tags_with_values.id,
        expertise_tags=["Python"],
        personal_links=[],
        hidden_public_fields=["expertise_tags"],
    )
    await _make_profile(
        session,
        user_id=hidden_tags_empty.id,
        expertise_tags=[],
        personal_links=[],
        hidden_public_fields=["expertise_tags"],
    )
    await _make_profile(
        session,
        user_id=hidden_links_with_values.id,
        expertise_tags=[],
        personal_links=[
            {"label": "Profile", "url": "https://example.com/profile"},
        ],
        hidden_public_fields=["personal_links"],
    )
    await _make_profile(
        session,
        user_id=hidden_links_empty.id,
        expertise_tags=[],
        personal_links=[],
        hidden_public_fields=["personal_links"],
    )

    async with api_client(session, _context(999_999_993)) as client:
        responses = {
            user_id: await client.get(f"/api/v1/directory/{user_id}")
            for user_id in (
                hidden_tags_with_values.id,
                hidden_tags_empty.id,
                hidden_links_with_values.id,
                hidden_links_empty.id,
            )
        }

    data = {}
    for user_id, response in responses.items():
        assert response.status_code == 200, response.text
        data[user_id] = response.json()["data"]

    tags_with_values = data[hidden_tags_with_values.id]
    assert tags_with_values["expertise_tags"] is None
    assert tags_with_values["personal_links"] == []
    assert data[hidden_tags_empty.id]["expertise_tags"] is None
    assert data[hidden_tags_empty.id]["personal_links"] == []
    assert data[hidden_links_with_values.id]["expertise_tags"] == []
    assert data[hidden_links_with_values.id]["personal_links"] is None
    assert data[hidden_links_empty.id]["expertise_tags"] == []
    assert data[hidden_links_empty.id]["personal_links"] is None


async def test_directory_profile_self_view_uses_colleague_clipping(
    session: AsyncSession,
) -> None:
    user = await _make_user(
        session,
        username=f"self-preview-{uuid4().hex}",
        nickname="Self Preview",
    )
    await _make_profile(
        session,
        user_id=user.id,
        bio="Hidden from own colleague preview",
        seat="D-04",
        hidden_public_fields=["bio"],
    )

    async with api_client(session, _context(user.id)) as client:
        response = await client.get(f"/api/v1/directory/{user.id}")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["bio"] is None
    assert data["seat"] == "D-04"


async def test_directory_profile_disabled_and_deleted_users_return_404(
    session: AsyncSession,
) -> None:
    disabled = await _make_user(
        session,
        username=f"disabled-profile-{uuid4().hex}",
        nickname="Disabled Profile",
        status=UserStatus.DISABLED,
    )
    deleted = await _make_user(
        session,
        username=f"deleted-profile-{uuid4().hex}",
        nickname="Deleted Profile",
        deleted_at=datetime.now(UTC).replace(tzinfo=None),
    )

    async with api_client(session, _context(999_999_996)) as client:
        for user_id in (disabled.id, deleted.id):
            response = await client.get(f"/api/v1/directory/{user_id}")
            assert response.status_code == 404, response.text
            assert response.json()["code"] == 2000


async def test_directory_profile_missing_user_returns_404(
    session: AsyncSession,
) -> None:
    async with api_client(session, _context(999_999_995)) as client:
        response = await client.get("/api/v1/directory/999999999")

    assert response.status_code == 404, response.text
    assert response.json()["code"] == 2000
