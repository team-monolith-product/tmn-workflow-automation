"""발송 게이트 테스트 — 자리를 먼저 잡고 보내는 순서가 지켜지는지 본다."""

import datetime

import pytest

from tests.fakes_sheets import FakeWorksheet

from service.sms import ledger
from service.sms import send as sms_send
from service.sms import transport

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기", "var2": "x"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기", "var2": "y"},
]


@pytest.fixture
def ws(monkeypatch) -> FakeWorksheet:
    sheet = FakeWorksheet([ledger.HEADER])
    monkeypatch.setattr(ledger, "open_ledger", lambda _id: sheet)
    return sheet


def _sent(phone: str, code: str = "1000") -> list:
    """이미 발송된 것으로 시트에 남아 있는 한 줄."""
    return ["2026-08-05", "discord", phone, "가", "LMS", "K0", code, "b", "slack"]


def _run(monkeypatch, response=None, boom=None, **extra):
    captured = {}

    def fake_send(payload):
        captured.update(payload)
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        template_name="discord",
        rows=ROWS,
        requested_by="a@team-mono.com",
        entrypoint="slack",
        **extra,
    )
    return result, captured


def test_보내기_전에_자리를_잡는다(ws, monkeypatch):
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    # 벤더에 넘어간 대상이 시트에 잡힌 행과 일치한다.
    rows = ledger.read_rows(ws)
    assert len(rows) == 2
    assert {r["번호"] for r in rows} == {"01011111111", "01022222222"}
    assert result["sent"] == 2
    assert [t["to"] for t in payload["targets"]] == ["01011111111", "01022222222"]


def test_접수하면_코드와_키가_시트에_남는다(ws, monkeypatch):
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"})
    assert all(r["접수코드"] == "1000" for r in ledger.read_rows(ws))
    assert all(r["messageKey"] == "K1" for r in ledger.read_rows(ws))


def test_이미_보낸_번호는_대상에서_빠진다(ws, monkeypatch):
    ws.rows.append(_sent("01011111111"))
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1 and result["skipped"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]


def test_사람이_손으로_적은_줄도_존중한다(ws, monkeypatch):
    # 서버가 죽어 뿌리오 웹으로 직접 보낸 뒤 손으로 남긴 기록. 사람은 번호를
    # 하이픈까지 넣어 적는다 — 그 표기로도 대조돼야 두 번 가지 않는다.
    ws.rows.append(["", "discord", "010-1111-1111", "", "", "", "1000", "형관", ""])
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]


def test_전원_중복이면_벤더를_부르지_않는다(ws, monkeypatch):
    for phone in ("01011111111", "01022222222"):
        ws.rows.append(_sent(phone))
    result, payload = _run(monkeypatch, {"code": "1000"})

    assert result["sent"] == 0 and result["skipped"] == 2
    assert payload == {}


def test_진_행은_중복으로_표시한다(ws, monkeypatch):
    ws.rows.append(_sent("01011111111"))
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    codes = [r["접수코드"] for r in ledger.read_rows(ws)]
    assert "중복" in codes


def test_벤더가_거절하면_실패로_표시해_재시도를_연다(ws, monkeypatch):
    # 접수되지 않은 것이 확실할 때만 재시도를 연다.
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, boom=transport.PpurioError(500, "boom"))

    assert all(r["접수코드"] == "실패" for r in ledger.read_rows(ws))
    assert ledger.owners(ledger.read_rows(ws)) == {}


def test_타임아웃이면_접수코드를_비워_재시도를_막는다(ws, monkeypatch):
    # 벤더가 이미 접수하고 응답만 못 돌려줬을 수 있다. 여기서 '실패'로 찍으면
    # 그 행이 죽은 것으로 취급돼 다음 시도가 이기고, 같은 사람에게 두 번 간다.
    with pytest.raises(TimeoutError):
        _run(monkeypatch, boom=TimeoutError("read timed out"))

    assert all(r["접수코드"] == "" for r in ledger.read_rows(ws))
    assert len(ledger.owners(ledger.read_rows(ws))) == 2


def test_접수코드가_1000이_아니면_실패로_본다(ws, monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, {"code": "2000", "description": "invalid"})

    assert all(r["접수코드"] == "실패" for r in ledger.read_rows(ws))


def test_벤더_옵션은_그대로_통과한다(ws, monkeypatch):
    at = datetime.datetime.now() + datetime.timedelta(hours=1)
    _, payload = _run(
        monkeypatch,
        {"code": "1000", "messageKey": "K"},
        send_at=at,
        duplicateFlag="N",
    )
    # transport 가 모르는 필드도 벤더로 흘러간다.
    assert payload["duplicateFlag"] == "N"
    assert payload["refKey"] == "discord"


def test_LMS면_제목을_붙인다(ws, monkeypatch):
    long_body = "가" * 100  # EUC-KR 200byte — SMS 90byte 한도를 넘는다
    monkeypatch.setattr(
        transport, "send", lambda payload: {"code": "1000", "messageKey": "K"}
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=ROWS,
        content=long_body,
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    assert result["message_type"] == "LMS"


def test_즉석_문안도_보낼_수_있다(ws, monkeypatch):
    # 저장된 문안 파일 없이 이번에만 쓰는 본문. 이 경로가 사라지면
    # 급한 공지를 파일부터 만들어야 보낼 수 있게 된다.
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="adhoc",
        rows=ROWS,
        content="[*이름*]선생님, 오늘 일정이 변경되었습니다.",
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    assert result["sent"] == 2
    assert captured["content"].startswith("[*이름*]선생님")


def test_같은_번호가_두_번_들어와도_한_번만_보낸다(ws, monkeypatch):
    # 접지 않으면 claim 이 한 번호에 두 행을 만들고, 승자 행의 주인이 사라져
    # 그 번호는 이 캠페인에서 영영 발송되지 않는다.
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=[
            {"to": "010-1111-1111", "name": "가"},
            {"to": "01011111111", "name": "가(표기만 다름)"},
        ],
        content="[*이름*]님",
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    assert result["sent"] == 1
    assert [t["to"] for t in captured["targets"]] == ["01011111111"]
    assert len(ledger.read_rows(ws)) == 1


def test_예약이_3분보다_가까우면_시트를_건드리기_전에_막는다(ws, monkeypatch):
    # 벤더도 거부하지만 그 거부는 발송 시도 뒤에야 돌아온다. 그때는 이미
    # 자리를 잡은 뒤라 재시도가 막힌다.
    soon = datetime.datetime.now() + datetime.timedelta(minutes=1)
    with pytest.raises(ValueError, match="3분"):
        _run(monkeypatch, {"code": "1000"}, send_at=soon)

    assert ledger.read_rows(ws) == []


def test_예약이면_sendTime이_실린다(ws, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    at = datetime.datetime.now() + datetime.timedelta(hours=1)
    sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=ROWS,
        content="[*이름*]님",
        requested_by="a@team-mono.com",
        entrypoint="script",
        send_at=at,
    )
    # 뿌리오 sendTime 은 yyyy-MM-ddTHH:mm:ss 다.
    assert captured["sendTime"] == at.strftime("%Y-%m-%dT%H:%M:%S")


def test_예약_판정은_컨테이너_시계가_UTC여도_KST로_한다(monkeypatch):
    # 컨테이너에 TZ 가 없어 datetime.now() 는 UTC 다. 사람은 KST 벽시계로
    # 적으므로, 그대로 빼면 이미 지난 시각이 9시간 여유로 보여 통과한다.
    import zoneinfo

    now_kst = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Seoul")).replace(
        tzinfo=None
    )
    past = now_kst - datetime.timedelta(hours=1)
    with pytest.raises(ValueError, match="3분"):
        sms_send.reserve_time(past)


def test_문제를_한_번에_모아_돌려준다():
    # 하나씩 터뜨리면 고치고 다시 돌리고를 반복하게 된다.
    soon = datetime.datetime.now() + datetime.timedelta(minutes=1)
    problems = sms_send.check(
        [{"to": "010-123"}, {"to": "010-456"}],
        template_name="discord",
        send_at=soon,
    )
    assert sum("형식 오류" in p for p in problems) == 2
    assert any("3분" in p for p in problems)


def test_check가_막는_것과_send가_막는_것이_같다(ws, monkeypatch):
    # check 가 통과시킨 발송이 send_campaign 에서 터지면 안 된다. 검증이
    # 두 벌이면 사람에게 보여준 것과 실제로 막히는 것이 갈라진다.
    bad = [{"to": "010-123"}]
    assert sms_send.check(bad, template_name="discord")
    with pytest.raises(ValueError):
        sms_send.send_campaign(
            spreadsheet_id="S1",
            campaign="x",
            rows=bad,
            template_name="discord",
            requested_by="a@team-mono.com",
            entrypoint="slack",
        )


def test_중복은_차단_사유가_아니다():
    # 접어서 보내므로 발송은 된다. 대신 preview 가 몇 건 접었는지 센다.
    rows = [{"to": "010-1111-1111"}, {"to": "01011111111"}]
    assert sms_send.check(rows, template_name="discord") == []
    assert sms_send.preview(rows, "discord")["folded"] == 1


def test_문안이_없으면_거기서_멈춘다():
    # 문안이 없으면 길이도 치환도 볼 수 없다.
    assert sms_send.check(ROWS, template_name="없는문안") == [
        "문안 '없는문안' 없음. 사용 가능: discord"
    ]


def test_문안과_즉석본문을_동시에_주면_거른다():
    assert sms_send.check(ROWS, template_name="discord", content="둘 다")


def test_수신자가_없으면_거른다():
    assert sms_send.check([], template_name="discord") == ["수신자가 없습니다."]


def test_통과하면_빈_목록이다():
    assert sms_send.check(ROWS, template_name="discord") == []
