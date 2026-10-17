"""User self-profile application services."""

from typing import Any

from app.models.user_profile import UserProfile


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


__all__: tuple[str, ...] = ("profile_values",)
