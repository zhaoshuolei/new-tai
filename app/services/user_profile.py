"""User self-profile application services."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessError, ErrorCode
from app.models.user_profile import UserProfile
from app.repositories import department as department_repository
from app.repositories import user as user_repository
from app.repositories import user_profile as user_profile_repository
from app.schemas.profile import ProfileRead


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


__all__: tuple[str, ...] = ("get_my_profile", "profile_values")
