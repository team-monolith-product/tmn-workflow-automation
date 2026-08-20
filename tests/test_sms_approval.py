"""승인 흐름 테스트 — 사람이 누르기 전에는 절대 안 나가야 한다.

에이전트가 도구를 부르는 것만으로 문자가 나가면, 모델이 대화를 잘못 읽었을 때
되돌릴 방법이 없다. 그 경계가 이 파일이 지키는 전부다.
"""

import pytest

from service.sms import draft
from service.sms import send as sms_send

from app import sms

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기"},
]


class FakeClient:
    """슬랙 클라이언트가 받은 호출을 기록한다."""

    def __init__(self):
        self.posted = []
        self.updated = []
        self.ephemeral = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222"}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)

    async def chat_postEphemeral(self, **kwargs):
        self.ephemeral.append(kwargs)


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
def _clean_drafts():
    draft._DRAFTS.clear()
    yield
    draft._DRAFTS.clear()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def handlers():
    app = FakeApp()
    sms.register_sms_handlers(app)
    return app.actions


async def _draft(
    client, *, user="U1", targets=ROWS, campaign="discord", content="[*이름*]님"
):
    tool = sms.get_sms_tools(client, user, "C1", "111.000")[0]
    return await tool.ainvoke(
        {"content": content, "targets": targets, "campaign": campaign}
    )


def _body(draft_id, user="U1"):
    return {
        "actions": [{"value": draft_id}],
        "user": {"id": user},
        "container": {"channel_id": "C1", "message_ts": "111.222"},
    }


async def _ack():
    return None


@pytest.mark.asyncio
async def test_도구는_문자를_보내지_않는다(client, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("도구가 직접 발송했다")

    monkeypatch.setattr(sms_send, "send_campaign", boom)

    answer = await _draft(client)

    assert "초안" in answer
    assert len(client.posted) == 1
    assert len(draft._DRAFTS) == 1


@pytest.mark.asyncio
async def test_카드에_대상과_미리보기가_실린다(client):
    await _draft(client)

    blocks = client.posted[0]["blocks"]
    text = str(blocks)
    assert "가, 나" in text
    assert "가님" in text  # [*이름*] 치환된 미리보기
    assert "discord" in text


@pytest.mark.asyncio
async def test_보내기를_누르면_그때_나간다(client, handlers, monkeypatch):
    sent = {}

    def fake_send(**kwargs):
        sent.update(kwargs)
        return {"sent": 2, "skipped": 0, "message_key": "K1"}

    monkeypatch.setattr(sms_send, "send_campaign", fake_send)
    await _draft(client)
    draft_id = next(iter(draft._DRAFTS))

    await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert sent["campaign"] == "discord"
    assert [row["to"] for row in sent["rows"]] == ["010-1111-1111", "010-2222-2222"]
    assert sent["requested_by"] == "U1"
    assert "2명" in client.updated[0]["text"]


@pytest.mark.asyncio
async def test_남이_누르면_안_나간다(client, handlers, monkeypatch):
    # 되돌릴 수 없는 발송이라 요청자만 누를 수 있다.
    def boom(**kwargs):
        raise AssertionError("남이 눌렀는데 나갔다")

    monkeypatch.setattr(sms_send, "send_campaign", boom)
    await _draft(client, user="U1")
    draft_id = next(iter(draft._DRAFTS))

    await handlers[sms.APPROVE](_ack, _body(draft_id, user="U2"), client)

    assert client.ephemeral and "U1" in client.ephemeral[0]["text"]
    # 초안은 살아 있어야 요청자가 다시 누를 수 있다.
    assert draft_id in draft._DRAFTS


@pytest.mark.asyncio
async def test_두_번_누르면_한_번만_나간다(client, handlers, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sms_send,
        "send_campaign",
        lambda **kwargs: calls.append(kwargs)
        or {"sent": 2, "skipped": 0, "message_key": "K"},
    )
    await _draft(client)
    draft_id = next(iter(draft._DRAFTS))

    await handlers[sms.APPROVE](_ack, _body(draft_id), client)
    await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert len(calls) == 1
    assert "만료" in client.updated[1]["text"]


@pytest.mark.asyncio
async def test_취소하면_아무것도_안_나간다(client, handlers, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("취소했는데 나갔다")

    monkeypatch.setattr(sms_send, "send_campaign", boom)
    await _draft(client)
    draft_id = next(iter(draft._DRAFTS))

    await handlers[sms.CANCEL](_ack, _body(draft_id), client)

    assert draft._DRAFTS == {}
    assert "취소" in client.updated[0]["text"]


@pytest.mark.asyncio
async def test_취소한_초안은_눌러도_안_나간다(client, handlers, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("취소된 초안이 나갔다")

    monkeypatch.setattr(sms_send, "send_campaign", boom)
    await _draft(client)
    draft_id = next(iter(draft._DRAFTS))
    await handlers[sms.CANCEL](_ack, _body(draft_id), client)

    await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert "만료" in client.updated[1]["text"]


@pytest.mark.asyncio
async def test_번호가_틀리면_초안을_안_올린다(client):
    answer = await _draft(client, targets=[{"to": "010-123"}])

    assert "고칠 것" in answer
    assert client.posted == []
    assert draft._DRAFTS == {}


@pytest.mark.asyncio
async def test_요청자를_모르면_초안을_안_올린다(client):
    tool = sms.get_sms_tools(client, None, "C1", "111.000")[0]

    answer = await tool.ainvoke({"content": "안녕", "targets": ROWS})

    assert "요청자" in answer
    assert client.posted == []


@pytest.mark.asyncio
async def test_발송이_터지면_카드에_사유가_남는다(client, handlers, monkeypatch):
    # 접수 여부를 모르는 실패는 사람이 뿌리오 웹에서 확인해야 한다.
    def boom(**kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(sms_send, "send_campaign", boom)
    await _draft(client)
    draft_id = next(iter(draft._DRAFTS))

    with pytest.raises(TimeoutError):
        await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert "TimeoutError" in client.updated[0]["text"]


@pytest.mark.asyncio
async def test_CS는_campaign_없이_올린다(client, handlers, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        sms_send,
        "send_campaign",
        lambda **kwargs: sent.update(kwargs)
        or {"sent": 1, "skipped": 0, "message_key": "K"},
    )
    await _draft(client, campaign=None, targets=ROWS[:1])
    draft_id = next(iter(draft._DRAFTS))

    await handlers[sms.APPROVE](_ack, _body(draft_id), client)

    assert sent["campaign"] is None
