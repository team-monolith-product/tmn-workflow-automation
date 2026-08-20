"""발송 게이트 테스트 — 자리를 먼저 잡고 보내는 순서가 지켜지는지 본다."""

import datetime
import urllib.error

import pytest

from tests.fakes_log import FakeLog

from service.sms import KST
from service.sms import send as sms_send
from service.sms import transport

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기", "var2": "x"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기", "var2": "y"},
]


@pytest.fixture
def store(monkeypatch) -> FakeLog:
    """sms_send 를 대신하는 가짜 로그."""
    fake = FakeLog()
    monkeypatch.setattr(sms_send, "log", fake)
    return fake


def _kst_now() -> datetime.datetime:
    """사람이 예약 시각을 적을 때 보는 벽시계.

    naive now() 를 쓰면 컨테이너(UTC)와 KST 로 판정하는 코드가 9시간 어긋난다.
    """
    return datetime.datetime.now(KST).replace(tzinfo=None)


def _run(
    monkeypatch, response=None, boom=None, *, rows=ROWS, campaign="discord", **extra
):
    """벤더를 목킹하고 send_campaign 을 부른다."""
    captured = {}

    def fake_send(payload):
        captured.update(payload)
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    if "content" not in extra:
        extra["template_name"] = "discord"
    result = sms_send.send_campaign(campaign=campaign, rows=rows, **extra)
    return result, captured


def test_보내기_전에_자리를_잡는다(store, monkeypatch):
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 2
    assert [t["to"] for t in payload["targets"]] == ["01011111111", "01022222222"]
    assert store.stages("01011111111") == ["발송"]


def test_이미_보낸_번호는_대상에서_빠진다(store, monkeypatch):
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"})
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K2"})

    assert result["sent"] == 0 and result["skipped"] == 2
    assert payload == {}


def test_한_명만_새로_들어오면_그_사람만_보낸다(store, monkeypatch):
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"}, rows=ROWS[:1])
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K2"})

    assert result["sent"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]


def test_CS는_같은_사람에게_여러_번_간다(store, monkeypatch):
    # campaign 이 None 이면 중복 차단을 받지 않는다. 컬럼 모델에서 CS 를
    # 예외로 빼야 했던 이유가 여기서 사라진다.
    _run(monkeypatch, {"code": "1000"}, campaign=None, rows=ROWS[:1], content="1차")
    result, _ = _run(
        monkeypatch, {"code": "1000"}, campaign=None, rows=ROWS[:1], content="2차"
    )

    assert result["sent"] == 1
    assert store.stages("01011111111") == ["발송", "발송"]


def test_벤더가_거절하면_실패로_남겨_재시도를_연다(store, monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, boom=transport.PpurioError(400, "bad request"))

    assert store.stages("01011111111") == ["실패"]
    result, _ = _run(monkeypatch, {"code": "1000", "messageKey": "K"})
    assert result["sent"] == 2


@pytest.mark.parametrize(
    "boom",
    [
        TimeoutError("read timed out"),
        urllib.error.HTTPError("u", 504, "gateway timeout", {}, None),
        ConnectionResetError("peer reset"),
    ],
    ids=["timeout", "504", "reset"],
)
def test_접수_여부를_모르면_발송중으로_남겨_재시도를_막는다(store, monkeypatch, boom):
    # 504 는 게이트웨이 타임아웃이라 소켓 타임아웃과 같은 취급이어야 한다.
    with pytest.raises(type(boom)):
        _run(monkeypatch, boom=boom)

    assert store.stages("01011111111") == ["모름"]
    result, _ = _run(monkeypatch, {"code": "1000"})
    assert result["sent"] == 0


def test_접수코드가_1000이_아니면_실패로_본다(store, monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, {"code": "2000", "description": "invalid"})

    assert store.stages("01011111111") == ["실패"]


def test_같은_번호가_두_번_들어와도_한_번만_보낸다(store, monkeypatch):
    result, payload = _run(
        monkeypatch,
        {"code": "1000", "messageKey": "K"},
        rows=[
            {"to": "010-1111-1111", "name": "가"},
            {"to": "01011111111", "name": "가(표기만 다름)"},
        ],
        content="[*이름*]님",
    )

    assert result["sent"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01011111111"]


def test_수신자가_없으면_DB를_건드리지_않는다(store, monkeypatch):
    monkeypatch.setattr(transport, "send", lambda payload: {"code": "1000"})

    with pytest.raises(ValueError, match="수신자가 없습니다"):
        sms_send.send_campaign(campaign="discord", rows=[], content="본문")

    assert store.rows == []


def test_LMS면_제목을_붙인다(store, monkeypatch):
    long_body = "가" * 100  # EUC-KR 200byte — SMS 90byte 한도를 넘는다
    result, payload = _run(
        monkeypatch, {"code": "1000", "messageKey": "K"}, content=long_body
    )

    assert result["message_type"] == "LMS"
    assert payload["subject"] == "discord"


def test_벤더_옵션은_그대로_통과한다(store, monkeypatch):
    at = _kst_now() + datetime.timedelta(hours=1)
    _, payload = _run(
        monkeypatch,
        {"code": "1000", "messageKey": "K"},
        send_at=at,
        duplicateFlag="N",
    )

    assert payload["duplicateFlag"] == "N"
    assert payload["sendTime"] == at.strftime("%Y-%m-%dT%H:%M:%S")


def test_예약이면_나갈_시각을_따로_기록한다(store, monkeypatch):
    # 접수 시각(sent_at)과 나갈 시각(scheduled_for)은 다르다. 하나로 뭉치면
    # 아직 안 나간 문자가 "그때 발송됨"으로 읽힌다.
    at = _kst_now() + datetime.timedelta(hours=3)
    _run(monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=at)

    assert store.rows[0]["scheduled_for"] == at
    assert store.rows[0]["sent_at"] is not None


def test_오프셋을_붙여도_벤더와_기록이_같은_시각을_본다(store, monkeypatch):
    aware = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=aware)

    kst = aware.astimezone(KST).replace(tzinfo=None)
    assert payload["sendTime"] == kst.strftime("%Y-%m-%dT%H:%M:%S")
    assert store.rows[0]["scheduled_for"] == kst


def test_예약이_3분보다_가까우면_DB를_건드리기_전에_막는다(store, monkeypatch):
    soon = _kst_now() + datetime.timedelta(minutes=1)
    with pytest.raises(ValueError, match="3분"):
        _run(monkeypatch, {"code": "1000"}, send_at=soon)

    assert store.rows == []


def test_예약_판정은_컨테이너_시계가_UTC여도_KST로_한다():
    past = _kst_now() - datetime.timedelta(hours=1)
    with pytest.raises(ValueError, match="3분"):
        sms_send.reserve_time(past)


def test_취소가_거절되면_성공으로_보지_않는다(monkeypatch):
    monkeypatch.setattr(
        transport, "cancel", lambda key: {"code": "7000", "description": "too late"}
    )

    with pytest.raises(transport.PpurioError):
        sms_send.cancel_reserved("K1")


def test_요청자와_채널이_기록된다(store, monkeypatch):
    _run(
        monkeypatch,
        {"code": "1000", "messageKey": "K"},
        channel_id="C123",
        requested_by="a@team-mono.com",
    )

    assert store.rows[0]["requested_by"] == "a@team-mono.com"
    assert store.rows[0]["channel_id"] == "C123"


def test_보낸_문안과_치환값이_남는다(store, monkeypatch):
    # 원문만 남기면 나중에 [*이름*] 자리가 빈 채로 보인다. 그 사람이 실제로
    # 받은 문자를 되살리려면 치환값도 있어야 한다.
    _run(monkeypatch, {"code": "1000"}, content="[*이름*]선생님, [*1*] 안내")

    assert store.rows[0]["content"] == "[*이름*]선생님, [*1*] 안내"
    assert store.rows[0]["variables"] == {"name": "가", "var1": "1기", "var2": "x"}


def test_문제를_한_번에_모아_돌려준다():
    soon = _kst_now() + datetime.timedelta(minutes=1)
    problems = sms_send.check(
        [{"to": "010-123"}, {"to": "010-456"}],
        template_name="discord",
        send_at=soon,
    )

    assert sum("형식 오류" in p for p in problems) == 2
    assert any("3분" in p for p in problems)


def test_check가_막는_것과_send가_막는_것이_같다(store, monkeypatch):
    bad = [{"to": "010-123"}]
    assert sms_send.check(bad, template_name="discord")
    with pytest.raises(ValueError):
        _run(monkeypatch, {"code": "1000"}, rows=bad)


def test_중복은_차단_사유가_아니다():
    rows = [{"to": "010-1111-1111"}, {"to": "01011111111"}]
    assert sms_send.check(rows, template_name="discord") == []
    assert sms_send.preview(rows, "discord")["folded"] == 1


def test_문안이_없으면_거기서_멈춘다():
    assert sms_send.check(ROWS, template_name="없는문안")


def test_문안과_즉석본문을_동시에_주면_거른다():
    assert sms_send.check(ROWS, template_name="discord", content="둘 다")
