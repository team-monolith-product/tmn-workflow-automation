"""수집 채널 등록 검증 테스트."""

from service.knowledge.register import validate_public_channel


def test_공개_채널은_통과한다():
    assert validate_public_channel({"is_channel": True, "is_private": False}) is None


def test_비공개_채널은_거절한다():
    assert validate_public_channel({"is_channel": True, "is_private": True})


def test_채널이_아니면_거절한다():
    assert validate_public_channel({"is_channel": False, "is_im": True})
