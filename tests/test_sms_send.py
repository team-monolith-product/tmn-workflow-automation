"""발송 게이트 테스트 — 자리를 먼저 잡고 보내는 순서가 지켜지는지 본다."""

import pytest

from tests.conftest_sms import FakeWorksheet

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
    monkeypatch.setattr(ledger, "open_ledger", lambda: sheet)
    return sheet


def _run(monkeypatch, response=None, boom=None, **extra):
    captured = {}

    def fake_send(payload, token=None):
        captured.update(payload)
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    result = sms_send.send_campaign(
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
    ws.rows.append(
        [
            "2026-08-05",
            "discord",
            "01011111111",
            "가",
            "LMS",
            "K0",
            "1000",
            "",
            "b",
            "slack",
        ]
    )
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1 and result["skipped"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]


def test_사람이_손으로_적은_줄도_존중한다(ws, monkeypatch):
    # 서버가 죽어 뿌리오 웹으로 직접 보낸 뒤 손으로 남긴 기록.
    ws.rows.append(
        ["", "discord", "01011111111", "", "", "", "1000", "수기", "형관", ""]
    )
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]


def test_전원_중복이면_벤더를_부르지_않는다(ws, monkeypatch):
    for phone in ("01011111111", "01022222222"):
        ws.rows.append(
            ["", "discord", phone, "", "LMS", "K0", "1000", "", "b", "slack"]
        )
    result, payload = _run(monkeypatch, {"code": "1000"})

    assert result["sent"] == 0 and result["skipped"] == 2
    assert payload == {}


def test_진_행은_중복으로_표시한다(ws, monkeypatch):
    ws.rows.append(
        ["", "discord", "01011111111", "", "LMS", "K0", "1000", "", "b", "slack"]
    )
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    codes = [r["접수코드"] for r in ledger.read_rows(ws)]
    assert "중복" in codes


def test_벤더가_실패하면_실패로_표시해_재시도를_연다(ws, monkeypatch):
    with pytest.raises(RuntimeError):
        _run(monkeypatch, boom=transport.PpurioError(500, "boom"))

    assert all(r["접수코드"] == "실패" for r in ledger.read_rows(ws))
    # 실패 행은 죽은 것으로 보므로 다음 시도가 이긴다.
    assert ledger.owners(ledger.read_rows(ws)) == {}


def test_접수코드가_1000이_아니면_실패로_본다(ws, monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, {"code": "2000", "description": "invalid"})

    assert all(r["접수코드"] == "실패" for r in ledger.read_rows(ws))


def test_벤더_옵션은_그대로_통과한다(ws, monkeypatch):
    _, payload = _run(
        monkeypatch,
        {"code": "1000", "messageKey": "K"},
        sendTime="2026-08-13T09:00:00",
    )
    # transport 가 모르는 필드도 벤더로 흘러간다.
    assert payload["sendTime"] == "2026-08-13T09:00:00"
    assert payload["refKey"] == "discord"


def test_LMS면_제목을_붙인다(ws, monkeypatch):
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})
    # 실제 discord 문안은 500byte 를 넘어 항상 LMS 다.
    assert payload["messageType"] == "LMS" and payload["subject"] == "discord"
