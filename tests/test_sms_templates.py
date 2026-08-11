"""문자 문안·수신자 정규화 테스트"""

import pytest

from service.sms import templates

TEMPLATE = "안녕하세요 [*이름*]선생님\n- 기수 : [*1*]\n- 링크 : [*2*]"


def test_길이는_euckr_기준으로_잰다():
    # UTF-8 로 재면 한글이 3바이트라 실제보다 길게 나온다.
    assert templates.euckr_len("한글") == 4
    assert templates.euckr_len("ab") == 2


def test_수신번호에서_하이픈을_걷어낸다():
    assert templates.normalize_phone("010-1234-5678") == "01012345678"
    assert templates.normalize_phone(" 010 1234 5678 ") == "01012345678"


def test_자릿수가_안_맞으면_거절한다():
    with pytest.raises(ValueError):
        templates.normalize_phone("010-123")


def test_이름과_변수를_치환한다():
    row = {"to": "01012345678", "name": "홍길동", "var1": "1기", "var2": "https://x"}
    assert templates.render(TEMPLATE, row) == (
        "안녕하세요 홍길동선생님\n- 기수 : 1기\n- 링크 : https://x"
    )


def test_빠진_변수는_빈_문자열로_둔다():
    # 태그가 그대로 남으면 그 문자열이 실제로 발송된다.
    assert "[*2*]" not in templates.render(TEMPLATE, {"to": "01012345678"})


def test_타입은_치환_후_최댓값으로_정한다():
    # 짧은 사람 기준으로 SMS 를 고르면 긴 사람만 발송에 실패한다.
    short = {"to": "01012345678", "name": "김", "var1": "1", "var2": "x"}
    long = {"to": "01012345679", "name": "김" * 60, "var1": "1", "var2": "x"}
    assert templates.decide_message_type(TEMPLATE, [short]) == "SMS"
    assert templates.decide_message_type(TEMPLATE, [short, long]) == "LMS"


def test_LMS_한도를_넘으면_거절한다():
    huge = {"to": "01012345678", "name": "가" * 1100, "var1": "", "var2": ""}
    with pytest.raises(ValueError):
        templates.decide_message_type(TEMPLATE, [huge])


def test_targets에_changeWord를_싣는다():
    rows = [{"to": "010-1234-5678", "name": "홍길동", "var1": "1기", "var2": "링크"}]
    assert templates.build_targets(rows) == [
        {
            "to": "01012345678",
            "name": "홍길동",
            "changeWord": {"var1": "1기", "var2": "링크"},
        }
    ]


def test_값이_없으면_changeWord를_넣지_않는다():
    assert templates.build_targets([{"to": "01012345678"}]) == [{"to": "01012345678"}]


def test_같은_본문은_같은_해시다():
    row = {"to": "01012345678", "name": "홍길동", "var1": "1기", "var2": "x"}
    other = {"to": "01099999999", "name": "홍길동", "var1": "1기", "var2": "x"}
    assert templates.content_hash(TEMPLATE, row) == templates.content_hash(
        TEMPLATE, other
    )


def test_실제_문안이_읽힌다():
    body = templates.load("discord")
    assert "[*이름*]" in body and "[*1*]" in body and "[*2*]" in body


def test_없는_문안은_사용_가능_목록을_알려준다():
    with pytest.raises(FileNotFoundError, match="discord"):
        templates.load("존재하지않는문안")


def test_즉석_문안은_본문을_그대로_쓴다():
    """데이터를 보고 그 자리에서 쓴 문안은 파일 없이 보낼 수 있어야 한다."""
    assert (
        templates.resolve(None, "이번 주 출석 안내입니다\n")
        == "이번 주 출석 안내입니다"
    )


def test_저장된_문안은_파일에서_읽는다():
    assert "[*이름*]" in templates.resolve("discord", None)


def test_문안은_파일과_본문_중_하나만_받는다():
    """둘 다 주면 어느 쪽이 나갔는지 사후에 알 수 없다."""
    with pytest.raises(ValueError):
        templates.resolve("discord", "본문")
    with pytest.raises(ValueError):
        templates.resolve(None, None)
