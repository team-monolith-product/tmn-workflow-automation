"""승인 흐름 테스트 — 누르기 전에는 안 나가야 한다."""

import pytest

from service.sms import send as sms_send
from service.sms import transport

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


def _press(client, action_id):
    """카드에 실제로 실린 버튼을 누른다.

    _DRAFTS 키를 손으로 넣으면, 카드가 엉뚱한 value 를 싣고 있어도 테스트가
    통과한다. 운영에서는 그때 [보내기] 가 늘 "이미 처리된 초안" 만 뱉는다.
    """
    button = next(
        element
        for element in client.posted[-1]["blocks"][1]["elements"]
        if element["action_id"] == action_id
    )
    return {
        "actions": [{"value": button["value"]}],
        "user": {"id": "U1"},
        "container": {"channel_id": "C1", "message_ts": "111.222"},
    }


async def _ack():
    return None


async def _click(handlers, action_id, body, client):
    # Bolt 는 파라미터 이름을 보고 kwargs 로 주입한다. 위치 인자로 부르면
    # 이름을 바꿔도 테스트가 통과하고 운영에서만 리스너가 안 붙는다.
    await handlers[action_id](ack=_ack, body=body, client=client)


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

    await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

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
    body = _press(client, sms.APPROVE)

    await _click(handlers, sms.APPROVE, body, client)
    await _click(handlers, sms.APPROVE, body, client)

    assert len(calls) == 1


async def test_취소하면_안_나간다(client, handlers, monkeypatch):
    monkeypatch.setattr(sms_send, "send", lambda **kw: pytest.fail("취소했는데 나갔다"))
    await _draft(client)
    approve, cancel = _press(client, sms.APPROVE), _press(client, sms.CANCEL)

    await _click(handlers, sms.CANCEL, cancel, client)
    await _click(handlers, sms.APPROVE, approve, client)

    assert "취소" in client.updated[0]["text"]


async def test_발송_중인_초안을_취소로_덮지_않는다(client, handlers, monkeypatch):
    # pop 결과를 안 보면, 이미 나가는 중인데 카드가 "취소했습니다" 로 바뀌어
    # 누른 사람이 막았다고 믿는다.
    monkeypatch.setattr(sms_send, "send", lambda **kw: {"sent": 2, "message_key": "K"})
    await _draft(client)
    approve, cancel = _press(client, sms.APPROVE), _press(client, sms.CANCEL)
    await _click(handlers, sms.APPROVE, approve, client)

    await _click(handlers, sms.CANCEL, cancel, client)

    assert "취소" not in client.updated[1]["text"]


async def test_번호가_틀리면_초안을_안_올린다(client):
    answer = await _draft(client, targets=[{"to": "010-123"}])

    assert "고칠 것" in answer
    assert client.posted == []


async def test_번호_없는_대상도_모아서_알린다(client):
    # 모델이 to 를 빼먹거나 선행 0 없는 수로 주는 일이 실제로 있다.
    answer = await _draft(client, targets=[{"to": 1011111111}])

    assert "고칠 것" in answer
    assert client.posted == []


async def test_벤더가_거절하면_안_나갔다고_말한다(client, handlers, monkeypatch):
    def boom(**kwargs):
        raise transport.PpurioError(400, "invalid sender")

    monkeypatch.setattr(sms_send, "send", boom)
    await _draft(client)

    with pytest.raises(transport.PpurioError):
        await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert "안 나갔습니다" in client.updated[0]["text"]


async def test_타임아웃은_안_나갔다고_말하지_않는다(client, handlers, monkeypatch):
    # 뿌리오가 이미 접수했을 수 있다. "실패" 라고 하면 다시 보내고, 같은
    # 사람이 두 번 받는다.
    def boom(**kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(sms_send, "send", boom)
    await _draft(client)

    with pytest.raises(TimeoutError):
        await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    text = client.updated[0]["text"]
    assert "안 나갔" not in text and "실패" not in text
    assert "모릅니다" in text
