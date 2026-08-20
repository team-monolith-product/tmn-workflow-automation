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


def test_0으로_시작하지_않으면_거절한다():
    # 모델이 to 를 JSON 수로 내면 선행 0 이 사라져 자릿수만 맞는 값이 온다.
    with pytest.raises(ValueError):
        templates.normalize_phone("1011111111")


def test_이름과_변수를_치환한다():
    row = {"to": "01012345678", "name": "홍길동", "var1": "1기", "var2": "https://x"}
    assert templates.render(TEMPLATE, row) == (
        "안녕하세요 홍길동선생님\n- 기수 : 1기\n- 링크 : https://x"
    )


def test_빠진_변수는_빈_문자열로_둔다():
    # 태그가 그대로 남으면 그 문자열이 실제로 발송된다.
    assert "[*2*]" not in templates.render(TEMPLATE, {"to": "01012345678"})


def test_targets에_changeWord를_싣는다():
    rows = [{"to": "01012345678", "name": "홍길동", "var1": "1기", "var2": "링크"}]
    assert templates.build_targets(TEMPLATE, rows) == [
        {
            "to": "01012345678",
            "name": "홍길동",
            "changeWord": {"var1": "1기", "var2": "링크"},
        }
    ]


def test_값이_없어도_문안이_쓰는_태그는_싣는다():
    # 키를 빼면 벤더가 치환할 것을 못 찾아 그 사람만 [*이름*]선생님 을 받는다.
    # 미리보기는 첫 사람만 보여주므로 승인자가 그것을 못 본다.
    assert templates.build_targets(TEMPLATE, [{"to": "01012345678"}]) == [
        {"to": "01012345678", "name": "", "changeWord": {"var1": "", "var2": ""}}
    ]


def test_문안이_안_쓰는_태그는_싣지_않는다():
    rows = [{"to": "01012345678", "name": "홍길동", "var1": "1기"}]
    assert templates.build_targets("안녕하세요", rows) == [{"to": "01012345678"}]
