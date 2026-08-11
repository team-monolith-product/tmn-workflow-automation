"""
승인 후 발송 → 도달 확인 → 보고 흐름 테스트
"""

import datetime

import pytest

from app import sms_approval
from service.sms import result as sms_result
from service.sms import send as sms_send


class FakeClient:
    """스레드에 올라간 메시지를 모아두는 슬랙 클라이언트"""

    def __init__(self):
        self.messages = []
        self.updates = []

    async def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs["text"])
        return {"ts": "1.1"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs["text"])
        return {"ts": kwargs["ts"]}


def draft(targets):
    """발송 초안 한 건"""
    return sms_approval.Draft(
        campaign="discord",
        targets=targets,
        requested_by="bae@team-mono.com",
        channel="C1",
        thread_ts="1.0",
        spreadsheet_id="S1",
        content="[*이름*]님 안내드립니다",
    )


@pytest.fixture
def no_wait(monkeypatch):
    """폴링 대기를 없앱니다."""
    monkeypatch.setattr(sms_approval, "POLL_INTERVAL_SECONDS", 0)


def _accepted(rows):
    return {
        "requested": len(rows),
        "skipped": 0,
        "sent": len(rows),
        "sent_to": [row["to"].replace("-", "") for row in rows],
        "code": "1000",
        "message_key": "key",
        "message_type": "SMS",
    }


def _page(rows: list[tuple[str, str, str]]) -> str:
    """발송결과 페이지를 흉내낸 표. rows 는 (일시, 수신번호, 결과)."""
    body = "".join(
        f"<tr><td>{at}</td><td>{phone}</td><td>{status}</td></tr>"
        for at, phone, status in rows
    )
    return (
        "<table><tr><th>발송일시</th><th>수신번호</th><th>결과</th></tr>"
        f"{body}</table>"
    )


async def test_도달과_실패를_나눠_보고한다(monkeypatch, no_wait):
    """실패분은 보고만 한다. 자동 재발송은 파서를 실물로 보정한 뒤에 켠다."""
    sent_calls = []

    def fake_send(*, campaign, rows, **_kwargs):
        sent_calls.append((campaign, [row["to"] for row in rows]))
        return _accepted(rows)

    async def fake_fetch(phones, sent_after=None):
        return {"01011112222": sms_result.DELIVERED, "01033334444": sms_result.FAILED}

    monkeypatch.setattr(sms_send, "send_campaign", fake_send)
    monkeypatch.setattr(sms_result, "record", lambda sheet, campaign, statuses: [])
    monkeypatch.setattr(sms_result, "fetch_results", fake_fetch)

    client = FakeClient()
    await sms_approval._send_and_report(
        draft(
            [
                {"to": "010-1111-2222", "name": "가"},
                {"to": "010-3333-4444", "name": "나"},
            ]
        ),
        "U1",
        client,
    )

    assert sent_calls == [("discord", ["010-1111-2222", "010-3333-4444"])]
    assert "도달 1건" in client.messages[-1]
    assert "도달 실패 1건" in client.messages[-1]


async def test_unconfirmed_is_reported_not_resent(monkeypatch, no_wait):
    """결과가 안 잡힌 번호는 재발송하지 않고 미확정으로 보고합니다."""
    sent_calls = []

    def fake_send(*, campaign, rows, **_kwargs):
        sent_calls.append(campaign)
        return _accepted(rows)

    async def fake_fetch(phones, sent_after=None):
        return {}

    monkeypatch.setattr(sms_send, "send_campaign", fake_send)
    monkeypatch.setattr(sms_result, "record", lambda sheet, campaign, statuses: [])
    monkeypatch.setattr(sms_result, "fetch_results", fake_fetch)

    client = FakeClient()
    await sms_approval._send_and_report(
        draft([{"to": "010-1111-2222", "name": "가"}]), "U1", client
    )

    assert sent_calls == ["discord"]
    assert "결과 미확정 1건" in client.messages[-1]


async def test_이미_보낸_번호는_도달_확인에서_기다리지_않는다(monkeypatch, no_wait):
    """send_campaign 이 걸러낸 번호를 기다리면 오지 않을 결과를 붙든다."""
    asked = []

    def fake_send(*, campaign, rows, **_kwargs):
        # 두 명 중 한 명은 이미 보낸 적이 있어 빠졌다.
        return {**_accepted(rows), "sent": 1, "skipped": 1, "sent_to": ["01033334444"]}

    async def fake_fetch(phones, sent_after=None):
        asked.append(list(phones))
        return {"01033334444": sms_result.DELIVERED}

    monkeypatch.setattr(sms_send, "send_campaign", fake_send)
    monkeypatch.setattr(sms_result, "record", lambda sheet, campaign, statuses: [])
    monkeypatch.setattr(sms_result, "fetch_results", fake_fetch)

    await sms_approval._send_and_report(
        draft(
            [
                {"to": "010-1111-2222", "name": "가"},
                {"to": "010-3333-4444", "name": "나"},
            ]
        ),
        "U1",
        FakeClient(),
    )

    # 한 번 만에 확정돼 폴링이 끝난다. 대상 전체를 기다렸다면 5회를 돌았다.
    assert asked == [["01033334444"]]


async def test_지난_발송의_결과를_이번_것으로_읽지_않는다(monkeypatch, no_wait):
    """발송결과 페이지는 누적이다. 실물 파서를 태워서 확인한다.

    mock 이 라운드별로 다른 dict 를 돌려주면 실물에 없는 분리를 가정하게 되고,
    바로 그 버그를 지목하는 테스트가 통과한다.
    """
    now = datetime.datetime.now()
    old = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    fresh = now.strftime("%Y-%m-%d %H:%M")

    # 같은 번호가 지난주엔 실패, 이번엔 성공으로 남아 있다.
    html = _page(
        [
            (fresh, "010-1111-2222", "수신성공"),
            (old, "010-1111-2222", "수신실패"),
        ]
    )

    scoped = sms_result.parse_results(html, now - datetime.timedelta(minutes=2))
    assert scoped == {"01011112222": sms_result.DELIVERED}

    # 범위를 안 주면 지난주 실패가 섞여 들어온다 — 그래서 sent_after 가 필수다.
    everything = sms_result.parse_results(html)
    assert everything == {"01011112222": sms_result.DELIVERED}


async def test_본문에_박힌_전화번호를_수신번호로_읽지_않는다():
    """실제 discord 문안 하단에 담당자 번호가 들어 있다."""
    html = (
        "<table><tr><th>발송일시</th><th>수신번호</th><th>내용</th><th>결과</th></tr>"
        "<tr><td>2026-08-11 10:00</td><td>010-1111-2222</td>"
        "<td>문의: 팀모노리스 최형관 팀장 010-7764-7538</td><td>수신성공</td></tr>"
        "</table>"
    )
    assert sms_result.parse_results(html) == {"01011112222": sms_result.DELIVERED}


async def test_본문의_실패라는_낱말이_상태를_뒤집지_않는다():
    html = (
        "<table><tr><th>발송일시</th><th>수신번호</th><th>내용</th><th>결과</th></tr>"
        "<tr><td>2026-08-11 10:00</td><td>010-1111-2222</td>"
        "<td>신청 실패하신 분들께 안내드립니다</td><td>수신성공</td></tr>"
        "</table>"
    )
    assert sms_result.parse_results(html) == {"01011112222": sms_result.DELIVERED}


async def test_표를_못_읽으면_조용히_비우지_않고_터뜨린다():
    """빈 결과는 '아직 결과가 안 올라왔다'와 구분되지 않아 영원히 안 들킨다."""
    with pytest.raises(sms_result.ResultPageChanged):
        sms_result.parse_results("<html><body>로그인이 필요합니다</body></html>")


async def test_일시_열이_없으면_범위를_요구했을_때_터뜨린다():
    html = (
        "<table><tr><th>수신번호</th><th>결과</th></tr>"
        "<tr><td>010-1111-2222</td><td>수신성공</td></tr></table>"
    )
    assert sms_result.parse_results(html) == {"01011112222": sms_result.DELIVERED}
    with pytest.raises(sms_result.ResultPageChanged):
        sms_result.parse_results(html, datetime.datetime.now())


async def test_approve_once(monkeypatch):
    """같은 초안을 두 번 승인해도 발송은 한 번만 시작됩니다."""
    started = []

    async def fake_send_and_report(draft_arg, approver, client):
        started.append(approver)

    monkeypatch.setattr(sms_approval, "_send_and_report", fake_send_and_report)
    pending = draft([{"to": "010-1111-2222", "name": "가"}])
    sms_approval._DRAFTS[pending.id] = pending

    approved, first = await sms_approval.approve_draft(pending.id, "U1", FakeClient())
    again, second = await sms_approval.approve_draft(pending.id, "U2", FakeClient())

    assert approved and "발송을 시작합니다" in first
    assert not again and "이미 처리" in second


async def test_만료된_초안은_승인으로_표시하지_않는다():
    """실패인데 '✅ 승인'을 붙이면 누른 사람이 발송된 줄 안다."""
    approved, message = await sms_approval.approve_draft("없는초안", "U1", FakeClient())
    assert not approved and "만료" in message


async def test_초안_id는_매번_다르다():
    """개수로 만들면 승인·만료로 줄어들 때 옛 카드와 충돌합니다."""
    ids = {draft([{"to": "010-1111-2222"}]).id for _ in range(50)}
    assert len(ids) == 50
