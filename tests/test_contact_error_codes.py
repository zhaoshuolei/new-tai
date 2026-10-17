from app.core.errors import ErrorCode
from app.models.contact import ContactStatus


def test_contact_error_codes_match_spec() -> None:
    assert ErrorCode.CUSTOMER_CONTACT_NOT_FOUND == 7005
    assert ErrorCode.CONTACT_FUNCTION_NOT_FOUND == 7006
    assert ErrorCode.CONTACT_FUNCTION_NAME_EXISTS == 7007
    assert ErrorCode.CONTACT_FUNCTION_REPLACEMENT_REQUIRED == 7008
    assert ErrorCode.CONTACT_FUNCTION_REPLACEMENT_INVALID == 7009
    assert ErrorCode.CONTACT_FUNCTION_UNAVAILABLE == 7010
    assert ErrorCode.CUSTOMER_DELETED_CREDIT_CODE_EXISTS == 7011
    assert ErrorCode.RECORD_NOT_DELETED == 7012


def test_contact_status_values() -> None:
    assert ContactStatus.ACTIVE == "ACTIVE"
    assert ContactStatus.DELETED == "DELETED"
