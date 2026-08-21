"""승인 흐름 테스트 — 누르기 전에는 안 나가야 한다."""

import pytest

from service.sms import history
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


def _ok(**extra):
    """뿌리오가 접수한 응답. send() 가 실제로 돌려주는 모양이다."""
    return {
        "sent": 2,
        "message_key": "K1",
        "message_type": "SMS",
        "content": "[*이름*]선생님",
        "targets": [
            {"to": "01011111111", "name": "가"},
            {"to": "01022222222", "name": "나"},
        ],
        **extra,
    }


@pytest.fixture(autouse=True)
def recorded(monkeypatch, client):
    """이력 저장을 잡아둔다. 안 막으면 테스트가 실제 DB 에 붙는다.

    남긴 시점에 카드가 이미 갱신됐는지 같이 담는다. 순서가 반대면 DB 가
    터졌을 때 이미 나간 발송을 카드가 실패로 그린다.
    """
    calls = []
    monkeypatch.setattr(
        history, "record", lambda **kw: calls.append((kw, len(client.updated)))
    )
    return calls


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
    actions = next(
        block for block in client.posted[-1]["blocks"] if block["type"] == "actions"
    )
    button = next(
        element for element in actions["elements"] if element["action_id"] == action_id
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


def _card(client) -> str:
    return str(client.posted[-1]["blocks"])


async def test_카드는_치환_전_원문을_보여준다(client):
    # 치환 후만 보여주면 "[*이름*] 팀장" 이 되어야 할 자리에 수신자 이름이
    # 박혀 있어도 문장이 자연스러워서 승인자가 못 잡는다.
    await _draft(client, content="[*이름*]선생님, 문의는 최형관 팀장")

    assert "[*이름*]선생님, 문의는 최형관 팀장" in _card(client)


async def test_카드에_번호와_치환값이_짝지어_실린다(client):
    # 존재 여부만 보면 짝이 뒤바뀌어도 통과한다. 이 카드의 존재 이유가 짝이다.
    await _draft(client)

    card = _card(client)
    assert "01011111111  가" in card
    assert "01022222222  나" in card


async def test_문안의_백틱이_카드를_깨지_않는다(client):
    # 펜스가 거기서 닫히면 나머지가 mrkdwn 으로 렌더돼 [*이름*] 이 굵은
    # 글씨가 된다. 실명이 박힌 사고와 화면상 구분이 안 된다.
    await _draft(client, content="```[*이름*]선생님```")

    card = client.posted[-1]["blocks"][0]["text"]["text"]
    assert "[*이름*]선생님" in card
    assert card.count("```") == 2


async def test_값의_개행과_백틱이_표를_깨지_않는다(client):
    # 개행은 한 행을 두 줄로 쪼개 딱 "한 칸 밀림" 처럼 보이고, 백틱은
    # 코드펜스를 닫아버린다. 둘 다 모델이 만든 값에서 나올 수 있다.
    await _draft(
        client,
        targets=[
            {"to": "010-1111-1111", "name": "가\r\n나"},
            {"to": "010-2222-2222", "name": "```"},
        ],
    )

    table = client.posted[-1]["blocks"][1]["text"]["text"]
    # \r 도 줄을 나눈다. \n 만 막으면 \r\n 값에서 그대로 쪼개진다.
    assert "\r" not in table
    assert "01011111111  가  나" in table
    assert "```" not in table.replace("```", "", 2)


async def test_수신자가_많으면_접되_마지막_한_줄은_남긴다(client):
    # 이름이 한 칸씩 밀리는 사고는 앞줄만 보면 안 보이고 끝에서 티가 난다.
    targets = [{"to": f"010-1111-{i:04d}", "name": f"이름{i}"} for i in range(1, 31)]
    await _draft(client, targets=targets)

    card = _card(client)
    assert "이름30" in card
    assert "명 접음" in card


async def test_보내기를_누르면_그때_나간다(client, handlers, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        sms_send,
        "send",
        lambda **kw: sent.update(kw) or _ok(),
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
        lambda **kw: calls.append(kw) or _ok(),
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
    assert len(client.updated) == 1


async def test_처리된_초안은_결과_카드를_건드리지_않는다(client, handlers, monkeypatch):
    # 이 PR 에는 이력 DB 가 없어 결과 카드가 messageKey 의 유일한 기록이다.
    # 남아 있는 버튼을 다시 눌렀다고 그것을 덮으면 기록이 사라진다.
    monkeypatch.setattr(sms_send, "send", lambda **kw: _ok())
    await _draft(client)
    approve, cancel = _press(client, sms.APPROVE), _press(client, sms.CANCEL)
    await _click(handlers, sms.APPROVE, approve, client)

    await _click(handlers, sms.CANCEL, cancel, client)
    await _click(handlers, sms.APPROVE, approve, client)

    assert len(client.updated) == 1
    assert "messageKey" in client.updated[0]["text"]


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


async def test_치환값이_길어도_슬랙_한도를_넘지_않는다(client):
    # 넘으면 슬랙이 invalid_blocks 로 거절해 카드가 아예 안 올라간다.
    targets = [
        {
            "to": f"010-1111-{i:04d}",
            "name": f"이름{i}",
            "var1": "https://x/" + "a" * 300,
        }
        for i in range(1, 31)
    ]
    await _draft(client, targets=targets, content="[*이름*] [*1*]")

    for block in client.posted[-1]["blocks"]:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000
    # 길이만 보면, 자르면서 접음 표시와 마지막 줄을 같이 날려도 통과한다.
    card = _card(client)
    assert "명 접음" in card
    assert "이름30" in card


async def test_보내면_이력을_남긴다(client, handlers, monkeypatch, recorded):
    monkeypatch.setattr(sms_send, "send", lambda **kw: _ok())
    await _draft(client, content="\n[*이름*]선생님\n")

    await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert len(recorded) == 1
    kwargs, updates_before = recorded[0]
    assert kwargs["channel_id"] == "C1"
    assert kwargs["thread_ts"] == "111.000"
    # 도구가 받은 날것이 아니라 벤더로 나간 문안을 남긴다.
    assert kwargs["content"] == "[*이름*]선생님"
    assert kwargs["approved_by"] == "U1"
    # 카드를 먼저 고치고 남긴다.
    assert updates_before == 1


async def test_안_나갔으면_이력을_남기지_않는다(
    client, handlers, monkeypatch, recorded
):
    def boom(**kwargs):
        raise transport.PpurioError(400, "invalid sender")

    monkeypatch.setattr(sms_send, "send", boom)
    await _draft(client)

    with pytest.raises(transport.PpurioError):
        await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert recorded == []
