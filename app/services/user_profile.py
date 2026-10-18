"""User self-profile application services."""

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_context import AuthContext
from app.core.errors import BusinessError, ErrorCode
from app.models.user_profile import PUBLIC_PROFILE_FIELD_KEYS, UserProfile
from app.repositories import department as department_repository
from app.repositories import user as user_repository
from app.repositories import user_profile as user_profile_repository
from app.schemas.profile import PrivacyUpdate, ProfileRead, ProfileUpdate
from app.services.audit import audited

_UNSET = object()
_JSON_LIST_FIELDS = frozenset({"expertise_tags", "personal_links"})


def profile_values(profile: UserProfile | None) -> dict[str, Any]:
    """Return all profile fields, normalizing a missing row and null JSON."""
    if profile is None:
        return {
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

    return {
        "signature": profile.signature,
        "bio": profile.bio,
        "work_phone": profile.work_phone,
        "extension": profile.extension,
        "work_email": profile.work_email,
        "office_location": profile.office_location,
        "seat": profile.seat,
        "expertise_tags": (
            profile.expertise_tags if profile.expertise_tags is not None else []
        ),
        "personal_links": (
            profile.personal_links if profile.personal_links is not None else []
        ),
        "hidden_public_fields": (
            profile.hidden_public_fields
            if profile.hidden_public_fields is not None
            else []
        ),
        "personal_phone": profile.personal_phone,
        "birthday": profile.birthday,
        "emergency_contact_name": profile.emergency_contact_name,
        "emergency_contact_phone": profile.emergency_contact_phone,
    }


async def get_my_profile(session: AsyncSession, *, user_id: int) -> ProfileRead:
    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    profile = await user_profile_repository.get_by_user_id(session, user_id)
    department_name: str | None = None
    if user.department_id is not None:
        department = await department_repository.get_by_id(
            session,
            user.department_id,
            include_deleted=True,
        )
        department_name = department.name if department is not None else None

    values = profile_values(profile)
    return ProfileRead(
        id=user.id,
        username=user.username,
        account_email=user.email,
        department_id=user.department_id,
        department_name=department_name,
        user_status=user.status,
        feishu_open_id=user.feishu_open_id,
        nickname=user.nickname,
        avatar=user.avatar,
        signature=values["signature"],
        bio=values["bio"],
        work_phone=values["work_phone"],
        extension=values["extension"],
        work_email=values["work_email"],
        office_location=values["office_location"],
        seat=values["seat"],
        expertise_tags=values["expertise_tags"],
        personal_links=values["personal_links"],
        hidden_public_fields=values["hidden_public_fields"],
        personal_phone=values["personal_phone"],
        birthday=values["birthday"],
        emergency_contact_name=values["emergency_contact_name"],
        emergency_contact_phone=values["emergency_contact_phone"],
    )


def _is_duplicate_profile(error: IntegrityError) -> bool:
    args = getattr(error.orig, "args", ())
    return bool(args and args[0] == 1062)


async def _get_or_create_profile(
    session: AsyncSession,
    user_id: int,
) -> UserProfile:
    profile = await user_profile_repository.get_by_user_id(session, user_id)
    if profile is not None:
        return profile

    candidate = UserProfile(user_id=user_id)
    try:
        async with session.begin_nested():
            await user_profile_repository.add(session, candidate)
        return candidate
    except IntegrityError as error:
        if not _is_duplicate_profile(error):
            raise
        profile = await user_profile_repository.lock_by_user_id(session, user_id)
        if profile is None:
            raise
        return profile


@audited(
    action="user_profile:update",
    resource_type="user_profile",
    resource_id_param="user_id",
)
async def update_my_profile(
    session: AsyncSession,
    payload: ProfileUpdate,
    *,
    user_id: int,
    context: AuthContext,
) -> ProfileRead:
    provided = payload.model_dump(exclude_unset=True)

    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    nickname = provided.pop("nickname", _UNSET)
    nickname_provided = nickname is not _UNSET
    if nickname_provided:
        new_nickname = nickname if isinstance(nickname, str) else None
        if not new_nickname and not user.username:
            # 通讯录靠昵称或登录名辨认员工；飞书账号没有登录名，清空昵称会让
            # 该员工在通讯录里变成占位名且无法被检索到。
            raise BusinessError(
                ErrorCode.INVALID_PARAMETER,
                "该账号没有登录名，昵称不能为空",
                http_status=422,
            )
        user.nickname = new_nickname

    if provided:
        profile = await _get_or_create_profile(session, user_id)
        for field, value in provided.items():
            if field in _JSON_LIST_FIELDS:
                setattr(profile, field, value if isinstance(value, list) else [])
            else:
                setattr(profile, field, value)

    await session.commit()
    if nickname_provided:
        await session.refresh(user)
    return await get_my_profile(session, user_id=user_id)


@audited(
    action="user_profile:privacy",
    resource_type="user_profile",
    resource_id_param="user_id",
)
async def update_my_privacy(
    session: AsyncSession,
    payload: PrivacyUpdate,
    *,
    user_id: int,
    context: AuthContext,
) -> ProfileRead:
    requested_fields = set(payload.hidden_fields)
    unknown_fields = requested_fields - set(PUBLIC_PROFILE_FIELD_KEYS)
    if unknown_fields:
        raise BusinessError(
            ErrorCode.INVALID_PARAMETER,
            "Unknown profile field",
            http_status=422,
        )

    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise BusinessError(
            ErrorCode.USER_NOT_FOUND,
            "User not found",
            http_status=404,
        )

    profile = await _get_or_create_profile(session, user_id)
    profile.hidden_public_fields = [
        key for key in PUBLIC_PROFILE_FIELD_KEYS if key in requested_fields
    ]
    await session.commit()
    return await get_my_profile(session, user_id=user_id)


replace_privacy = update_my_privacy


__all__: tuple[str, ...] = (
    "get_my_profile",
    "profile_values",
    "replace_privacy",
    "update_my_privacy",
    "update_my_profile",
)
