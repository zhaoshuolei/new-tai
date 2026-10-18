from app.core.errors import ErrorCode


def test_follow_up_error_codes_match_spec() -> None:
    assert ErrorCode.FOLLOW_UP_NOT_FOUND == 7013
    assert ErrorCode.FOLLOW_UP_METHOD_NOT_FOUND == 7014
    assert ErrorCode.FOLLOW_UP_METHOD_NAME_EXISTS == 7015
    assert ErrorCode.FOLLOW_UP_METHOD_IN_USE == 7016
    assert ErrorCode.FOLLOW_UP_NOT_OWNER == 7017
    assert ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND == 7018
    assert ErrorCode.FOLLOW_UP_ATTACHMENT_LIMIT == 7019
    assert ErrorCode.FOLLOW_UP_ATTACHMENT_INVALID == 7020
    assert ErrorCode.FOLLOW_UP_USER_UNAVAILABLE == 7021
