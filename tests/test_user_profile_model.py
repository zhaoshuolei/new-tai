"""Self-profile model and repository tests."""

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Date
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.user import User, UserStatus
from app.models.user_profile import PUBLIC_PROFILE_FIELD_KEYS, UserProfile
from app.repositories.user_profile import add, get_by_user_id
from app.services.user_profile import profile_values

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


async def _make_user(session: AsyncSession) -> User:
    user = User(
        username=f"profile-{uuid4().hex[:12]}",
        password_hash=None,
        status=UserStatus.ACTIVE,
        token_version=0,
    )
    session.add(user)
    await session.flush()
    return user


async def test_get_by_user_id_returns_saved_profile(session: AsyncSession) -> None:
    user = await _make_user(session)
    profile = await add(session, UserProfile(user_id=user.id))

    found = await get_by_user_id(session, user.id)

    assert found is profile


async def test_user_id_is_unique(session: AsyncSession) -> None:
    user = await _make_user(session)
    await add(session, UserProfile(user_id=user.id))

    with pytest.raises(IntegrityError) as error:
        await add(session, UserProfile(user_id=user.id))

    assert error.value.orig is not None
    assert error.value.orig.args[0] == 1062


async def test_json_columns_default_to_empty_lists(session: AsyncSession) -> None:
    user = await _make_user(session)
    user_id = user.id
    await add(session, UserProfile(user_id=user_id))
    session.expire_all()

    profile = await get_by_user_id(session, user_id)

    assert profile is not None
    assert profile.expertise_tags == []
    assert profile.personal_links == []
    assert profile.hidden_public_fields == []


def test_public_profile_field_keys_keep_canonical_order() -> None:
    assert PUBLIC_PROFILE_FIELD_KEYS == (
        "signature",
        "bio",
        "work_phone",
        "extension",
        "work_email",
        "office_location",
        "seat",
        "expertise_tags",
        "personal_links",
        "avatar",
    )


def test_birthday_uses_date_type() -> None:
    assert isinstance(UserProfile.__table__.c.birthday.type, Date)


def test_profile_values_returns_all_fields_raw() -> None:
    profile = UserProfile(
        user_id=1,
        signature="签名",
        bio="个人简介",
        work_phone="010-12345678",
        extension="801",
        work_email="employee@example.com",
        office_location="北京",
        seat="A-01",
        expertise_tags=["Python", "SQLAlchemy"],
        personal_links=[{"label": "主页", "url": "https://example.com"}],
        hidden_public_fields=["bio", "personal_links"],
        personal_phone="13800000000",
        birthday=date(1990, 1, 2),
        emergency_contact_name="张三",
        emergency_contact_phone="13900000000",
    )

    assert profile_values(profile) == {
        "signature": "签名",
        "bio": "个人简介",
        "work_phone": "010-12345678",
        "extension": "801",
        "work_email": "employee@example.com",
        "office_location": "北京",
        "seat": "A-01",
        "expertise_tags": ["Python", "SQLAlchemy"],
        "personal_links": [{"label": "主页", "url": "https://example.com"}],
        "hidden_public_fields": ["bio", "personal_links"],
        "personal_phone": "13800000000",
        "birthday": date(1990, 1, 2),
        "emergency_contact_name": "张三",
        "emergency_contact_phone": "13900000000",
    }


def test_profile_values_normalizes_missing_values() -> None:
    assert profile_values(None) == {
        "signature": None,
        "bio": None,
        "work_phone": None,
        "extension": None,
        "work_email": None,
        "office_location": None,
        "seat": None,
        "expertise_tags": [],
        "personal_links": [],
        "hidden_public_fields": [],
        "personal_phone": None,
        "birthday": None,
        "emergency_contact_name": None,
        "emergency_contact_phone": None,
    }


def test_profile_values_normalizes_null_json_columns() -> None:
    profile = UserProfile(user_id=1)
    profile.__dict__["expertise_tags"] = None
    profile.__dict__["personal_links"] = None
    profile.__dict__["hidden_public_fields"] = None

    values = profile_values(profile)

    assert values["expertise_tags"] == []
    assert values["personal_links"] == []
    assert values["hidden_public_fields"] == []
