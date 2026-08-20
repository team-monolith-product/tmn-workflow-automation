"""승인 흐름 테스트 — 누르기 전에는 안 나가야 한다."""

import pytest

from service.sms import send as sms_send

from app import sms

ROWS = [
    {"to": "010-1111-1111", "name": "가"},
    {"to": "010-2222-2222", "name": "나"},
]


class FakeClient:
    """슬랙 클라이언트가 받은 호출을 기록한다."""

    def __init__(self):
        self.posted = []
        self.updated = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222"}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)


class FakeApp:
    """@app.action 으로 등록된 핸들러를 붙잡아 둔다."""

    def __init__(self):
        self.actions = {}

    def action(self, action_id):
        def register(func):
            self.actions[action_id] = func
            return func

        return register


@pytest.fixture(autouse=True)
def _clean():
    sms._DRAFTS.clear()
    yield
    sms._DRAFTS.clear()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def handlers():
    app = FakeApp()
    sms.register_sms_handlers(app)
    return app.actions


async def _draft(client, targets=ROWS, content="[*이름*]님"):
    tool = sms.get_sms_tools(client, "C1", "111.000")[0]
    return await tool.ainvoke({"content": content, "targets": targets})


def _body(draft_id):
    return {
        "actions": [{"value": draft_id}],
        "user": {"id": "U1"},
        "container": {"channel_id": "C1", "message_ts": "111.222"},
    }


async def _ack():
    return None


async def test_도구는_문자를_보내지_않는다(client, monkeypatch):
    monkeypatch.setattr(
        sms_send, "send", lambda **kw: pytest.fail("도구가 직접 발송했다")
    )

    answer = await _draft(client)

    assert "초안" in answer
    assert len(client.posted) == 1
    assert len(sms._DRAFTS) == 1


async def test_카드에_미리보기가_실린다(client):
    await _draft(client)

    assert "가님" in str(client.posted[0]["blocks"])


async def test_보내기를_누르면_그때_나간다(client, handlers, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        sms_send,
        "send",
        lambda **kw: sent.update(kw) or {"sent": 2, "message_key": "K1"},
    )
    await _draft(client)

    await handlers[sms.APPROVE](_ack, _body(next(iter(sms._DRAFTS))), client)

    assert [row["to"] for row in sent["rows"]] == ["010-1111-1111", "010-2222-2222"]
    assert "2명" in client.updated[0]["text"]


async def test_두_번_누르면_한_번만_나간다(client, handlers, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sms_send,
        "send",
        lambda **kw: calls.append(kw) or {"sent": 2, "message_key": "K"},
    )
    await _draft(client)
    draft_id = next(iter(sms._DRAFTS))

    await handlers[sms.APPROVE](_ack, _body(draft_id), client)
    await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert len(calls) == 1


async def test_취소하면_안_나간다(client, handlers, monkeypatch):
    monkeypatch.setattr(sms_send, "send", lambda **kw: pytest.fail("취소했는데 나갔다"))
    await _draft(client)
    draft_id = next(iter(sms._DRAFTS))

    await handlers[sms.CANCEL](_ack, _body(draft_id), client)
    await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert "취소" in client.updated[0]["text"]


async def test_번호가_틀리면_초안을_안_올린다(client):
    answer = await _draft(client, targets=[{"to": "010-123"}])

    assert "고칠 것" in answer
    assert client.posted == []


async def test_발송이_터지면_카드에_사유가_남는다(client, handlers, monkeypatch):
    def boom(**kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(sms_send, "send", boom)
    await _draft(client)

    with pytest.raises(TimeoutError):
        await handlers[sms.APPROVE](_ack, _body(next(iter(sms._DRAFTS))), client)

    assert "TimeoutError" in client.updated[0]["text"]
