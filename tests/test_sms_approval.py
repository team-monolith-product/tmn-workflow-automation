"""
승인 후 발송 → 도달 확인 → 재발송 루프 테스트
"""

import pytest

from app import sms_approval
from service.sms import result as sms_result


class FakeClient:
    """스레드에 올라간 메시지를 모아두는 슬랙 클라이언트"""

    def __init__(self):
        self.messages = []

    async def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs["text"])
        return {"ts": "1.1"}


def draft(targets):
    """발송 초안 한 건"""
    return {
        "campaign": "discord",
        "template_name": None,
        "content": "[*이름*]님 안내드립니다",
        "targets": targets,
        "subject": None,
        "requested_by": "bae@team-mono.com",
        "channel": "C1",
        "thread_ts": "1.0",
    }


@pytest.fixture
def no_wait(monkeypatch):
    """폴링 대기를 없앱니다."""
    monkeypatch.setattr(sms_approval, "POLL_INTERVAL_SECONDS", 0)


def _accepted(count):
    return {
        "requested": count,
        "skipped": 0,
        "sent": count,
        "code": "1000",
        "message_key": "key",
        "message_type": "SMS",
    }


async def test_retries_only_failed_recipients_under_new_campaign(monkeypatch, no_wait):
    """도달 실패한 번호만, campaign 을 바꿔 재발송합니다.

    같은 campaign 으로는 UNIQUE 에 걸려 재발송 자체가 안 되므로 -r2 여야 합니다.
    """
    sent_calls = []

    def fake_send(campaign, template_name, content, targets, *args):
        sent_calls.append((campaign, [target["to"] for target in targets]))
        return _accepted(len(targets))

    statuses = [
        {"01011112222": sms_result.DELIVERED, "01033334444": sms_result.FAILED},
        {"01033334444": sms_result.DELIVERED},
    ]

    async def fake_fetch(phones):
        return statuses.pop(0)

    failures = [[{"to": "01033334444", "name": "나"}], []]

    def fake_record(campaign, resolved):
        return failures.pop(0)

    monkeypatch.setattr(sms_approval, "send_blocking", fake_send)
    monkeypatch.setattr(sms_approval, "record_blocking", fake_record)
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

    assert sent_calls == [
        ("discord", ["010-1111-2222", "010-3333-4444"]),
        ("discord-r2", ["01033334444"]),
    ]
    assert "도달 2건" in client.messages[-1]


async def test_unconfirmed_is_reported_not_resent(monkeypatch, no_wait):
    """결과가 안 잡힌 번호는 재발송하지 않고 미확정으로 보고합니다."""
    sent_calls = []

    def fake_send(campaign, template_name, content, targets, *args):
        sent_calls.append(campaign)
        return _accepted(len(targets))

    async def fake_fetch(phones):
        return {}

    monkeypatch.setattr(sms_approval, "send_blocking", fake_send)
    monkeypatch.setattr(sms_approval, "record_blocking", lambda campaign, resolved: [])
    monkeypatch.setattr(sms_result, "fetch_results", fake_fetch)

    client = FakeClient()
    await sms_approval._send_and_report(
        draft([{"to": "010-1111-2222", "name": "가"}]), "U1", client
    )

    assert sent_calls == ["discord"]
    assert "결과 미확정 1건" in client.messages[-1]


async def test_approve_once(monkeypatch):
    """같은 초안을 두 번 승인해도 발송은 한 번만 시작됩니다."""
    started = []

    async def fake_send_and_report(draft_arg, approver, client):
        started.append(approver)

    monkeypatch.setattr(sms_approval, "_send_and_report", fake_send_and_report)
    sms_approval._DRAFTS["d1"] = draft([{"to": "010-1111-2222", "name": "가"}])

    first = await sms_approval.approve_draft("d1", "U1", FakeClient())
    second = await sms_approval.approve_draft("d1", "U2", FakeClient())

    assert "발송을 시작합니다" in first
    assert "이미 처리" in second
