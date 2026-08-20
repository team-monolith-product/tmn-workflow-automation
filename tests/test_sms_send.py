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


def test_한_번의_요청으로_전원에게_보낸다(monkeypatch):
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 2
    assert payload["targetCount"] == 2
    assert [t["to"] for t in payload["targets"]] == ["01011111111", "01022222222"]


def test_치환값이_벤더로_넘어간다(monkeypatch):
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert payload["targets"][0]["changeWord"]["var1"] == "1기"
    assert payload["targets"][0]["name"] == "가"


def test_벤더로_나가는_것은_치환_전_원문이다(monkeypatch):
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert payload["content"] == CONTENT


def test_같은_번호는_접어서_한_번만_보낸다(monkeypatch):
    monkeypatch.setattr(
        transport, "send", lambda payload: {"code": "1000", "messageKey": "K"}
    )

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
    long_body = "가" * 100  # EUC-KR 200byte — SMS 90byte 한도를 넘는다
    monkeypatch.setattr(
        transport, "send", lambda payload: {"code": "1000", "messageKey": "K"}
    )

    result = sms_send.send(rows=ROWS, content=long_body, subject="공지")

    assert result["message_type"] == "LMS"


def test_치환값이_태그보다_짧아도_원문_길이로_판정한다(monkeypatch):
    # 벤더로 나가는 것은 원문이다. 치환 후만 재면 SMS 로 판정해 놓고
    # 90byte 넘는 원문을 보내게 된다.
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    # 원문 94byte, 치환 후 88byte 가 되도록 맞춘다.
    content = "[*이름*][*1*]" + "가" * 40
    rows = [{"to": "010-1111-1111", "name": "가", "var1": "나"}]

    sms_send.send(rows=rows, content=content, subject="공지")

    assert captured["messageType"] == "LMS"


def test_수신자가_없으면_거른다():
    assert sms_send.preview([], CONTENT)["problems"] == ["수신자가 없습니다."]


def test_문안이_비면_거른다():
    assert sms_send.preview(ROWS, "")["problems"] == ["문안이 비어 있습니다."]


def test_번호가_없거나_수여도_형식_오류로_모은다():
    # 모델이 만든 목록이라 to 가 빠지거나 수로 올 수 있다.
    problems = sms_send.preview([{"name": "홍길동"}, {"to": 1011111111}], CONTENT)[
        "problems"
    ]

    assert len(problems) == 1
    assert "형식 오류" in problems[0]


def test_문제를_한_번에_모아_돌려준다():
    problems = sms_send.preview([{"to": "010-123"}, {"to": "010-456"}], CONTENT)[
        "problems"
    ]

    assert sum("형식 오류" in p for p in problems) == 2


def test_보낼_수_있으면_problems가_비어_있다():
    assert sms_send.preview(ROWS, CONTENT)["problems"] == []


def test_미리보기는_치환한_본문을_보여준다():
    summary = sms_send.preview(ROWS, CONTENT)

    assert summary["sample"] == "가선생님, 1기 안내드립니다"
    assert summary["targets"] == 2
    assert summary["message_type"] == "SMS"


def test_보낼_수_없으면_send가_터진다(monkeypatch):
    monkeypatch.setattr(transport, "send", lambda payload: pytest.fail("막았어야 했다"))

    with pytest.raises(ValueError, match="형식 오류"):
        sms_send.send(rows=[{"to": "010-123"}], content=CONTENT)
