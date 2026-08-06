"""발송 게이트 테스트 — 자리를 먼저 잡고 보내는 순서가 지켜지는지 본다."""

import pytest

from service.sms import send as sms_send
from service.sms import transport


class FakeCursor:
    """execute 순서를 기록하는 커서. UNIQUE 충돌은 claimed 목록으로 흉내낸다."""

    def __init__(self, log: list, claimable: set):
        self.log = log
        self.claimable = claimable
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        head = sql.strip().split()[0].upper()
        self.log.append((head, params))
        if head == "INSERT":
            phone = params["phone"]
            self._last = (
                {"id": 100 + len(self.log), "phone": phone}
                if phone in self.claimable
                else None
            )
        elif head == "SELECT":
            self._last = {"total": 0}
        else:
            self._last = None

    def fetchone(self):
        return self._last


class FakeConn:
    def __init__(self, claimable: set):
        self.log: list = []
        self.commits = 0
        self.claimable = claimable

    def cursor(self):
        return FakeCursor(self.log, self.claimable)

    def commit(self):
        self.commits += 1
        self.log.append(("COMMIT", None))


ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기", "var2": "x"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기", "var2": "y"},
]


def _send(conn, monkeypatch, response=None, boom=None):
    sent = {}

    def fake_send(payload, token=None):
        sent["payload"] = payload
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    result = sms_send.send_campaign(
        conn,
        campaign="discord",
        template_name="discord",
        rows=ROWS,
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    return result, sent.get("payload")


def test_보내기_전에_INSERT_하고_커밋한다(monkeypatch):
    conn = FakeConn({"01011111111", "01022222222"})
    _send(conn, monkeypatch, {"code": "1000", "messageKey": "K1"})

    heads = [head for head, _ in conn.log]
    # INSERT 두 번 → COMMIT → (벤더 호출) → UPDATE
    assert heads[:3] == ["INSERT", "INSERT", "COMMIT"]
    assert heads.index("COMMIT") < heads.index("UPDATE")


def test_이미_보낸_번호는_대상에서_빠진다(monkeypatch):
    conn = FakeConn({"01011111111"})  # 두 번째는 UNIQUE 충돌
    result, payload = _send(conn, monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1
    assert result["skipped"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01011111111"]


def test_전원_중복이면_벤더를_부르지_않는다(monkeypatch):
    conn = FakeConn(set())
    result, payload = _send(conn, monkeypatch, {"code": "1000"})

    assert result["sent"] == 0 and result["skipped"] == 2
    assert payload is None
    assert "UPDATE" not in [head for head, _ in conn.log]


def test_벤더가_실패하면_자리를_반납한다(monkeypatch):
    conn = FakeConn({"01011111111", "01022222222"})
    with pytest.raises(RuntimeError):
        _send(conn, monkeypatch, boom=transport.PpurioError(500, "boom"))

    heads = [head for head, _ in conn.log]
    assert "DELETE" in heads and "UPDATE" not in heads


def test_접수코드가_1000이_아니면_반납한다(monkeypatch):
    conn = FakeConn({"01011111111", "01022222222"})
    with pytest.raises(transport.PpurioError):
        _send(conn, monkeypatch, {"code": "2000", "description": "invalid"})

    assert "DELETE" in [head for head, _ in conn.log]


def test_벤더_옵션은_그대로_통과한다(monkeypatch):
    conn = FakeConn({"01011111111", "01022222222"})
    captured = {}

    def fake_send(payload, token=None):
        captured.update(payload)
        return {"code": "1000", "messageKey": "K"}

    monkeypatch.setattr(transport, "send", fake_send)
    sms_send.send_campaign(
        conn,
        campaign="discord",
        template_name="discord",
        rows=ROWS,
        requested_by="a@team-mono.com",
        entrypoint="slack",
        sendTime="2026-08-13T09:00:00",
    )
    # transport 가 모르는 필드도 벤더로 흘러간다.
    assert captured["sendTime"] == "2026-08-13T09:00:00"
    assert captured["refKey"] == "discord"


def test_LMS면_제목을_붙인다(monkeypatch):
    conn = FakeConn({"01011111111", "01022222222"})
    _, payload = _send(conn, monkeypatch, {"code": "1000", "messageKey": "K"})
    # 실제 discord 문안은 500byte 를 넘어 항상 LMS 다.
    assert payload["messageType"] == "LMS"
    assert payload["subject"] == "discord"
