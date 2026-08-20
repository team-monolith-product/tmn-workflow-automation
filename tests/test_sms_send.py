"""발송 계층 테스트."""

import datetime

import pytest

from service.sms import KST
from service.sms import send as sms_send
from service.sms import transport

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기"},
]
CONTENT = "[*이름*]선생님, [*1*] 안내드립니다"


def _kst_now() -> datetime.datetime:
    """컨테이너(UTC)와 KST 판정이 어긋나지 않도록 벽시계를 맞춘다."""
    return datetime.datetime.now(KST).replace(tzinfo=None)


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


def test_벤더_옵션은_그대로_통과한다(monkeypatch):
    at = _kst_now() + datetime.timedelta(hours=1)
    _, payload = _run(
        monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=at, duplicateFlag="N"
    )

    assert payload["duplicateFlag"] == "N"
    assert payload["sendTime"] == at.strftime("%Y-%m-%dT%H:%M:%S")


def test_예약이_3분보다_가까우면_보내기_전에_막는다(monkeypatch):
    def boom(payload):
        raise AssertionError("막았어야 했다")

    monkeypatch.setattr(transport, "send", boom)
    soon = _kst_now() + datetime.timedelta(minutes=1)

    with pytest.raises(ValueError, match="3분"):
        sms_send.send(rows=ROWS, content=CONTENT, send_at=soon)


def test_예약_판정은_컨테이너_시계가_UTC여도_KST로_한다():
    past = _kst_now() - datetime.timedelta(hours=1)

    with pytest.raises(ValueError, match="3분"):
        sms_send.reserve_time(past)


def test_오프셋을_붙여도_KST로_읽는다():
    aware = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    kst = aware.astimezone(KST).replace(tzinfo=None)

    assert sms_send.reserve_time(aware) == kst.strftime("%Y-%m-%dT%H:%M:%S")


def test_취소가_거절되면_성공으로_보지_않는다(monkeypatch):
    monkeypatch.setattr(
        transport, "cancel", lambda key: {"code": "7000", "description": "too late"}
    )

    with pytest.raises(transport.PpurioError):
        sms_send.cancel_reserved("K1")


def test_문제를_한_번에_모아_돌려준다():
    soon = _kst_now() + datetime.timedelta(minutes=1)

    problems = sms_send.check([{"to": "010-123"}, {"to": "010-456"}], CONTENT, soon)

    assert sum("형식 오류" in p for p in problems) == 2
    assert any("3분" in p for p in problems)


def test_수신자가_없으면_거른다():
    assert sms_send.check([], CONTENT) == ["수신자가 없습니다."]


def test_문안이_비면_거른다():
    assert sms_send.check(ROWS, "") == ["문안이 비어 있습니다."]


def test_통과하면_빈_목록이다():
    assert sms_send.check(ROWS, CONTENT) == []


def test_미리보기는_치환한_본문을_보여준다():
    summary = sms_send.preview(ROWS, CONTENT)

    assert summary["sample"] == "가선생님, 1기 안내드립니다"
    assert summary["targets"] == 2
    assert summary["message_type"] == "SMS"
