from datetime import date, timedelta

import pytest
from pydantic import ValidationError

import app.schemas.profile as profile_schema
from app.schemas.profile import (
    PersonalLinkInput,
    PrivacyUpdate,
    ProfileUpdate,
    public_link_host,
)
from app.services.contact_validation import normalize_phone


def test_profile_update_is_partial_and_forbids_avatar() -> None:
    assert ProfileUpdate().model_dump(exclude_unset=True) == {}

    with pytest.raises(ValidationError) as error:
        ProfileUpdate(avatar="x")

    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_profile_update_field_names_are_exact() -> None:
    assert set(ProfileUpdate.model_fields) == {
        "nickname",
        "signature",
        "bio",
        "work_phone",
        "extension",
        "work_email",
        "office_location",
        "seat",
        "personal_phone",
        "emergency_contact_name",
        "emergency_contact_phone",
        "birthday",
        "expertise_tags",
        "personal_links",
    }


def test_extension_has_an_independent_rule() -> None:
    assert ProfileUpdate(extension="8042").extension == "8042"
    assert ProfileUpdate(extension="101").extension == "101"
    assert ProfileUpdate(extension="1" * 16).extension == "1" * 16

    with pytest.raises(ValidationError):
        ProfileUpdate(extension="12a4")
    with pytest.raises(ValidationError):
        ProfileUpdate(extension="1" * 17)

    # A three-digit extension is legitimate even though it is not a phone number.
    with pytest.raises(ValueError, match="invalid phone number"):
        normalize_phone("101")


def test_phone_fields_are_normalized() -> None:
    profile = ProfileUpdate(
        work_phone="138 0013 8000",
        personal_phone="139-0013-9000",
        emergency_contact_phone="+86 137 0013 7000",
    )

    assert profile.work_phone == "13800138000"
    assert profile.personal_phone == "13900139000"
    assert profile.emergency_contact_phone == "+8613700137000"

    with pytest.raises(ValidationError, match="invalid phone number"):
        ProfileUpdate(work_phone="123")


def test_work_email_is_normalized() -> None:
    profile = ProfileUpdate(work_email="  person@example.com  ")

    assert profile.work_email == "person@example.com"

    with pytest.raises(ValidationError, match="invalid email address"):
        ProfileUpdate(work_email="not-an-email")


@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("nickname", 100),
        ("signature", 100),
        ("bio", 2000),
        ("office_location", 100),
        ("seat", 50),
        ("emergency_contact_name", 50),
    ],
)
def test_text_length_caps(field: str, maximum: int) -> None:
    valid = ProfileUpdate.model_validate({field: "x" * maximum})
    assert getattr(valid, field) == "x" * maximum

    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({field: "x" * (maximum + 1)})


def test_expertise_tags_normalize_before_count_and_length_checks() -> None:
    profile = ProfileUpdate(expertise_tags=["Java", " Java ", ""])
    assert profile.expertise_tags == ["Java"]

    profile = ProfileUpdate(expertise_tags=["Java"] * 11)
    assert profile.expertise_tags == ["Java"]

    with pytest.raises(ValidationError):
        ProfileUpdate(expertise_tags=[f"tag-{index}" for index in range(11)])

    profile = ProfileUpdate(expertise_tags=["x" * 20])
    assert profile.expertise_tags == ["x" * 20]

    with pytest.raises(ValidationError):
        ProfileUpdate(expertise_tags=["x" * 21])


def test_personal_link_label_falls_back_to_host() -> None:
    profile = ProfileUpdate(
        personal_links=[{"label": "  ", "url": "https://a.example/x"}]
    )

    assert profile.personal_links is not None
    assert profile.personal_links[0].label == "a.example"
    assert public_link_host("https://a.example/x") == "a.example"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "/relative",
        "https://a.example/a b",
        "https://a.example/a\x00b",
        "https://a.example/a\\b",
        "https://user:pass@a.example/x",
        f"https://a.example/{'x' * 500}",
        "https://a.example:99999/x",
    ],
)
def test_personal_link_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ProfileUpdate(personal_links=[{"label": "Example", "url": url}])


def test_personal_link_url_length_cap() -> None:
    prefix = "https://a.example/"
    assert len(prefix) == 18

    profile = ProfileUpdate(
        personal_links=[{"label": "Example", "url": prefix + "x" * 482}]
    )
    assert profile.personal_links is not None

    with pytest.raises(ValidationError):
        ProfileUpdate(
            personal_links=[{"label": "Example", "url": prefix + "x" * 483}]
        )


def test_personal_link_label_cap_applies_after_fallback() -> None:
    profile = ProfileUpdate(
        personal_links=[{"label": "x" * 30, "url": "https://a.example/x"}]
    )
    assert profile.personal_links is not None

    with pytest.raises(ValidationError):
        ProfileUpdate(
            personal_links=[{"label": "x" * 31, "url": "https://a.example/x"}]
        )

    with pytest.raises(ValidationError):
        ProfileUpdate(
            personal_links=[
                {"label": "  ", "url": f"https://{'a' * 27}.com/x"},
            ]
        )


def test_personal_link_count_is_checked_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    four_links = [
        {"label": f"Link {index}", "url": f"https://a.example/{index}"}
        for index in range(4)
    ]
    assert len({link["url"] for link in four_links}) == 4

    with pytest.raises(ValidationError):
        ProfileUpdate(personal_links=four_links)

    monkeypatch.setattr(profile_schema, "public_link_host", lambda _: None)
    profile = ProfileUpdate(
        personal_links=[
            {"label": "  ", "url": "https://discarded.example/x"},
            *four_links[:3],
        ]
    )

    assert profile.personal_links is not None
    assert [link.url for link in profile.personal_links] == [
        link["url"] for link in four_links[:3]
    ]


def test_personal_links_do_not_deduplicate_urls() -> None:
    profile = ProfileUpdate(
        personal_links=[
            {"label": "Work", "url": "https://a.example/x"},
            {"label": "Docs", "url": "https://a.example/x"},
        ]
    )

    assert profile.personal_links is not None
    assert [link.label for link in profile.personal_links] == ["Work", "Docs"]


def test_personal_link_input_trims_fields() -> None:
    link = PersonalLinkInput(label="  Example  ", url="  https://a.example/x  ")

    assert link.label == "Example"
    assert link.url == "https://a.example/x"


def test_birthday_uses_business_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profile_schema, "business_today", lambda: date(2026, 10, 18))

    assert ProfileUpdate(birthday=date(2026, 10, 18)).birthday == date(2026, 10, 18)
    assert ProfileUpdate(birthday=date(1900, 1, 1)).birthday == date(1900, 1, 1)

    with pytest.raises(ValidationError, match="birthday must not be in the future"):
        ProfileUpdate(birthday=date(2026, 10, 18) + timedelta(days=1))


def test_nickname_blank_normalizes_to_none_and_length_is_capped() -> None:
    assert ProfileUpdate(nickname="   ").nickname is None

    with pytest.raises(ValidationError):
        ProfileUpdate(nickname="x" * 101)


def test_privacy_update_does_not_validate_the_service_whitelist() -> None:
    privacy = PrivacyUpdate(hidden_fields=["nickname"])

    assert privacy.hidden_fields == ["nickname"]

    with pytest.raises(ValidationError):
        PrivacyUpdate(hidden_fields=[], unknown="value")
