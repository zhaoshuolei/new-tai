import pytest

from app.services.contact_validation import normalize_email, normalize_phone


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_phone_becomes_none(raw: str | None) -> None:
    assert normalize_phone(raw) is None


def test_phone_strips_spaces_and_hyphens() -> None:
    assert normalize_phone("+86 138-0013-8000") == "+8613800138000"


def test_plain_mobile_is_accepted() -> None:
    assert normalize_phone("13800138000") == "13800138000"


@pytest.mark.parametrize("raw", ["12345", "abc1234567", "1" * 21, "+", "1+2345678"])
def test_invalid_phone_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_email_becomes_none(raw: str | None) -> None:
    assert normalize_email(raw) is None


def test_email_is_stripped_and_accepted() -> None:
    assert normalize_email(" a@b.com ") == "a@b.com"


@pytest.mark.parametrize(
    "raw", ["no-at-sign", "a@b", "a b@c.com", "x@" + "y" * 260 + ".com"]
)
def test_invalid_email_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(raw)
