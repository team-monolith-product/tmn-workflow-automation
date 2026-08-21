"""발송 계층 테스트."""

import pytest

from service.sms import send as sms_send
from service.sms import transport

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기"},
]
CONTENT = "[*이름*]선생님, [*1*] 안내드립니다"


def _run(monkeypatch, response=None, boom=None, **extra):
    captured = {}

    def fake_send(payload):
        captured.update(payload)
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    result = sms_send.send(rows=ROWS, content=CONTENT, **extra)
    return result, captured


def _accept(monkeypatch, captured=None):
    """벤더가 접수한 것으로 두고, 나간 payload 를 잡는다."""

    def fake_send(payload):
        if captured is not None:
            captured.update(payload)
        return {"code": "1000", "messageKey": "K"}

    monkeypatch.setattr(transport, "send", fake_send)


def test_한_번의_요청으로_전원에게_보낸다(monkeypatch):
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 2
    assert payload["targetCount"] == 2
    assert [t["to"] for t in payload["targets"]] == ["01011111111", "01022222222"]


def test_벤더_필수_필드가_빠지지_않는다(monkeypatch):
    # duplicateFlag·refKey 가 빠져 400 code 2000 으로 전건이 거절된 적이 있습니다(8/21).
    # 카드는 정상으로 보여 발송된 줄 알았습니다 — 실패가 눈에 띄지 않는 종류입니다.
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    for field in (
        "messageType",
        "content",
        "duplicateFlag",
        "refKey",
        "targetCount",
        "targets",
    ):
        assert field in payload, f"벤더 필수 필드가 빠졌다: {field}"


def test_치환값이_벤더로_넘어간다(monkeypatch):
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert payload["targets"][0]["changeWord"]["var1"] == "1기"
    assert payload["targets"][0]["name"] == "가"


def test_벤더로_나가는_것은_치환_전_원문이다(monkeypatch):
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert payload["content"] == CONTENT


def test_같은_번호는_접어서_한_번만_보낸다(monkeypatch):
    _accept(monkeypatch)

    result = sms_send.send(
        rows=[{"to": "010-1111-1111"}, {"to": "01011111111"}], content="안녕"
    )

    assert result["sent"] == 1


def test_벤더가_거절하면_터뜨린다(monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, boom=transport.PpurioError(400, "bad request"))


def test_접수코드가_1000이_아니면_실패로_본다(monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, {"code": "2000", "description": "invalid"})


def test_LMS면_제목을_붙인다(monkeypatch):
    # 뿌리오는 LMS 에 subject 가 없으면 거절한다. payload 를 안 보면 이 두
    # 줄을 지워도 전부 초록이고, 90byte 넘는 문안이 실계정에서만 터진다.
    long_body = "가" * 100  # EUC-KR 200byte — SMS 90byte 한도를 넘는다
    captured = {}
    _accept(monkeypatch, captured)

    result = sms_send.send(rows=ROWS, content=long_body)

    assert result["message_type"] == "LMS"
    assert captured["subject"] == "안내"


def test_제목을_주면_그것을_싣는다(monkeypatch):
    captured = {}
    _accept(monkeypatch, captured)

    sms_send.send(rows=ROWS, content="가" * 100, subject="공지")

    assert captured["subject"] == "공지"


def test_SMS면_제목을_붙이지_않는다(monkeypatch):
    captured = {}
    _accept(monkeypatch, captured)

    sms_send.send(rows=ROWS, content=CONTENT)

    assert "subject" not in captured


def test_치환값이_태그보다_짧아도_원문_길이로_판정한다(monkeypatch):
    # 벤더로 나가는 것은 원문이고 벤더도 원문 길이로 타입을 본다. 치환 후만
    # 재면 SMS 로 판정해 놓고 90byte 넘는 원문을 보내게 된다.
    captured = {}
    _accept(monkeypatch, captured)
    # 원문 93byte, 치환 후 84byte 가 되도록 맞춘다.
    content = "[*이름*][*1*]" + "가" * 40
    rows = [{"to": "010-1111-1111", "name": "가", "var1": "나"}]

    sms_send.send(rows=rows, content=content)

    assert captured["messageType"] == "LMS"


def test_타입은_치환_후_최댓값으로_정한다():
    # 짧은 사람 기준으로 SMS 를 고르면 긴 사람만 발송에 실패한다.
    short = {"to": "01012345678", "name": "김"}
    long = {"to": "01012345679", "name": "김" * 60}

    assert sms_send.preview([short], "[*이름*]선생님").message_type == "SMS"
    assert sms_send.preview([short, long], "[*이름*]선생님").message_type == "LMS"


def test_LMS_한도를_넘으면_거절한다():
    huge = {"to": "01012345678", "name": "가" * 1100}

    assert "LMS 한도" in sms_send.preview([huge], "[*이름*]선생님").problems[0]


def test_수신자가_없으면_거른다():
    assert sms_send.preview([], CONTENT).problems == ["수신자가 없습니다."]


def test_문안이_비면_거른다():
    assert sms_send.preview(ROWS, "").problems == ["문안이 비어 있습니다."]


def test_공백뿐인_문안도_비어_있는_것으로_본다():
    # 검사를 정규화보다 먼저 하면 "\n\n" 이 통과해 빈 문자가 발송된다.
    assert sms_send.preview(ROWS, "\n\n").problems == ["문안이 비어 있습니다."]
    assert sms_send.preview(ROWS, "   ").problems == ["문안이 비어 있습니다."]


def test_번호가_없거나_수여도_형식_오류로_모은다():
    # 모델이 만든 목록이라 to 가 빠지거나 수로 올 수 있다. JSON 수는 선행 0 을
    # 못 써서 1011111111 로 오는데, 자릿수만 보면 통과해 그대로 벤더로 나간다.
    plan = sms_send.preview([{"name": "홍길동"}, {"to": 1011111111}], CONTENT)

    assert len(plan.problems) == 2


def test_문제를_한_번에_모아_돌려준다():
    plan = sms_send.preview([{"to": "010-123"}, {"to": "010-456"}], CONTENT)

    assert sum("형식 오류" in p for p in plan.problems) == 2


def test_보낼_수_있으면_problems가_비어_있다():
    assert sms_send.preview(ROWS, CONTENT).problems == []


def test_미리보기는_치환한_본문을_보여준다():
    plan = sms_send.preview(ROWS, CONTENT)

    assert plan.sample == "가선생님, 1기 안내드립니다"
    assert len(plan.rows) == 2
    assert plan.message_type == "SMS"


def test_보낼_수_없으면_send가_터진다(monkeypatch):
    monkeypatch.setattr(transport, "send", lambda payload: pytest.fail("막았어야 했다"))

    with pytest.raises(ValueError, match="형식 오류"):
        sms_send.send(rows=[{"to": "010-123"}], content=CONTENT)
