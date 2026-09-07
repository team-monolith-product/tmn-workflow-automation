"""승인 흐름 테스트 — 누르기 전에는 안 나가야 한다."""

import asyncio
from datetime import datetime, timedelta

import pytest

from service.sms import history
from service.sms import send as sms_send
from service.sms import transport

from app import sms
from app.tools import python_tools
from app.tools.python_tools import get_execute_python_tool

ROWS = [
    {"to": "010-1111-1111", "name": "가"},
    {"to": "010-2222-2222", "name": "나"},
]


class FakeClient:
    """슬랙 클라이언트가 받은 호출을 기록한다."""

    def __init__(self):
        self.posted = []
        self.updated = []
        self.views = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "111.222"}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)

    async def views_open(self, **kwargs):
        self.views.append(kwargs["view"])


class FakeApp:
    """@app.action 으로 등록된 핸들러를 붙잡아 둔다."""

    def __init__(self):
        self.actions = {}
        self.views = {}

    def action(self, action_id):
        def register(func):
            self.actions[action_id] = func
            return func

        return register

    def view(self, callback_id):
        def register(func):
            self.views[callback_id] = func
            return func

        return register


def _ok(**extra):
    """뿌리오가 접수한 결과. 필드를 빠뜨리면 여기서 터진다."""
    return sms_send.Sent(
        ref_key="R1",
        sender="01077647538",
        message_key="K1",
        message_type="SMS",
        send_at=None,
        content="[*이름*]선생님",
        targets=[
            {"to": "01011111111", "name": "가"},
            {"to": "01022222222", "name": "나"},
        ],
    )._replace(**extra)


@pytest.fixture(autouse=True)
def recorded(monkeypatch, client):
    """이력 저장을 잡아둔다. 안 막으면 테스트가 실제 DB 에 붙는다.

    남긴 시점에 카드가 이미 갱신됐는지 같이 담는다. 순서가 반대면 DB 가
    터졌을 때 이미 나간 발송을 카드가 실패로 그린다.
    """
    calls = []
    monkeypatch.setattr(
        history,
        "record",
        lambda sent, **kw: calls.append((sent, kw, len(client.updated))),
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
    return {**app.actions, **app.views}


@pytest.fixture
def revised():
    """[수정] 피드백이 에이전트로 되돌아간 기록."""
    return []


@pytest.fixture
def handlers_with_revise(revised):
    app = FakeApp()

    async def revise(**kwargs):
        revised.append(kwargs)

    sms.register_sms_handlers(app, revise=revise)
    return {**app.actions, **app.views}


def _later(minutes: int) -> str:
    """지금부터 N 분 뒤(한국 시간). 고정 문자열은 날이 지나면 과거가 된다."""
    when = datetime.now(tz=sms_send.KST) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _code_tool(client):
    """코드가 draft_sms 를 부를 수 있는 실행 도구. 진짜 코루틴을 물린다."""
    return get_execute_python_tool(
        draft_sms=sms.get_draft_sms_tool(client, "C1", "111.000").coroutine
    )


async def _draft(client, targets=ROWS, content="[*이름*]님", send_at=""):
    tool = sms.get_draft_sms_tool(client, "C1", "111.000")
    return await tool.ainvoke(
        {"content": content, "targets": targets, "send_at": send_at}
    )


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
        "trigger_id": "T1",
    }


async def _ack():
    return None


def _submit(client, feedback: str) -> dict:
    """열린 모달에 피드백을 적어 낸다.

    private_metadata 를 손으로 만들지 않고 모달에 실린 값을 그대로 쓴다.
    엉뚱한 값을 싣고 있어도 통과하면, 운영에서는 카드가 안 바뀐다.
    """
    view = client.views[-1]
    return {
        "view": {
            "private_metadata": view["private_metadata"],
            "state": {
                "values": {
                    sms.FEEDBACK_BLOCK: {sms.FEEDBACK_INPUT: {"value": feedback}}
                }
            },
        },
        "user": {"id": "U1"},
    }


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
    # 남아 있는 버튼을 다시 눌렀다고 결과 카드를 덮으면, 누가 언제 보냈는지가
    # 스레드에서 사라진다.
    monkeypatch.setattr(sms_send, "send", lambda **kw: _ok())
    await _draft(client)
    approve, cancel = _press(client, sms.APPROVE), _press(client, sms.CANCEL)
    await _click(handlers, sms.APPROVE, approve, client)

    await _click(handlers, sms.CANCEL, cancel, client)
    await _click(handlers, sms.APPROVE, approve, client)

    assert len(client.updated) == 1
    assert "messageKey `K1`" in client.updated[0]["text"]


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
    sent, kwargs, updates_before = recorded[0]
    assert kwargs == {
        "channel_id": "C1",
        "thread_ts": "111.000",
        "approved_by": "U1",
    }
    # send() 가 돌려준 것을 그대로 넘긴다. 도구가 받은 날것이 아니다.
    assert sent.content == "[*이름*]선생님"
    # 카드를 먼저 고치고 남긴다.
    assert updates_before == 1


async def test_예약이면_예약_시각을_남긴다(client, handlers, monkeypatch, recorded):
    # 이게 안 남으면 다음 주에 나갈 문자가 오늘 나간 것처럼 보인다.
    when = datetime(2026, 8, 22, 9, 0, tzinfo=sms_send.KST)
    monkeypatch.setattr(sms_send, "send", lambda **kw: _ok(send_at=when))
    await _draft(client)

    await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert recorded[0][0].send_at == when


async def test_이력을_못_남겨도_카드는_보냈다고_남는다(
    client, handlers, monkeypatch, recorded
):
    # 카드를 먼저 고치는 이유가 이것이다. 순서가 반대면 이미 나간 발송을
    # 카드가 실패로 그린다.
    monkeypatch.setattr(sms_send, "send", lambda **kw: _ok())

    def boom(sent, **kwargs):
        raise RuntimeError("DB down")

    monkeypatch.setattr(history, "record", boom)
    await _draft(client)

    with pytest.raises(RuntimeError):
        await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert "보냈습니다" in client.updated[0]["text"]


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


async def test_예약이면_카드가_시각과_예약하기를_보여준다(client):
    # "보내기" 라고 쓰여 있으면 지금 나가는 줄 알고 누른다.
    await _draft(client, send_at=_later(60))

    card = _card(client)
    assert "예약" in card
    assert "예약하기" in card
    # 시간대가 없으면 어느 나라 9시인지 물어보게 된다.
    assert "KST" in card


async def test_예약이_없으면_보내기다(client):
    await _draft(client)

    assert "예약하기" not in _card(client)


async def test_예약을_승인하면_시각이_함께_나간다(client, handlers, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        sms_send,
        "send",
        lambda **kw: sent.update(kw)
        or _ok(send_at=datetime(2026, 8, 22, 9, 0, tzinfo=sms_send.KST)),
    )
    await _draft(client, send_at=_later(60))

    await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert sent["send_at"]
    assert "예약" in client.updated[0]["text"]


async def test_예약_시각이_지나면_안_나갔다고_말한다(client, handlers, monkeypatch):
    # 초안을 올린 뒤 승인까지 시간이 흐른다. 여기서 "접수 여부를 모릅니다" 로
    # 새면, 안 나간 것을 나갔을 수도 있다고 읽어 아무도 다시 보내지 않는다.
    def expired(**kwargs):
        raise ValueError("예약은 지금부터 3분 뒤부터 됩니다")

    monkeypatch.setattr(sms_send, "send", expired)
    await _draft(client, send_at=_later(60))

    await _click(handlers, sms.APPROVE, _press(client, sms.APPROVE), client)

    assert "안 나갔습니다" in client.updated[-1]["text"]


async def test_수정을_눌러도_초안은_살아_있다(client, handlers_with_revise):
    # 모달을 열어 놓고 닫아 버릴 수 있다. 그때 초안이 사라지면 멀쩡한
    # 카드의 [보내기] 가 "이미 처리된 초안" 만 뱉는 죽은 버튼이 된다.
    await _draft(client)

    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)

    assert len(client.views) == 1
    assert len(sms._DRAFTS) == 1


async def test_수정_모달은_고칠_문안을_보여준다(client, handlers_with_revise):
    # 무엇을 고치는지 안 보이면 피드백이 엉뚱한 초안에 붙는다.
    await _draft(client, content="[*이름*]선생님, 마감은 8월 22일입니다")

    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)

    assert "마감은 8월 22일입니다" in str(client.views[-1]["blocks"])


async def test_수정을_내면_피드백이_에이전트로_간다(
    client, handlers_with_revise, revised
):
    await _draft(client)
    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)

    await handlers_with_revise[sms.REVISE_VIEW](
        ack=_ack, body=_submit(client, "마감일을 8월 30일로"), client=client
    )

    assert revised[0]["text"] == "마감일을 8월 30일로"
    # 스레드를 벗어나면 에이전트가 앞의 대화를 못 읽어 처음부터 다시 묻는다.
    assert revised[0]["thread_ts"] == "111.000"
    assert "마감일을 8월 30일로" in client.updated[-1]["text"]


async def test_수정을_낸_뒤에는_옛_문안이_안_나간다(
    client, handlers_with_revise, monkeypatch
):
    # 새 초안이 따로 올라오므로, 옛 카드의 [보내기] 가 살아 있으면
    # 고쳐 달라고 해놓고 고치기 전 문안이 나간다.
    monkeypatch.setattr(sms_send, "send", lambda **kw: pytest.fail("옛 문안이 나갔다"))
    await _draft(client)
    approve_body = _press(client, sms.APPROVE)
    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)
    await handlers_with_revise[sms.REVISE_VIEW](
        ack=_ack, body=_submit(client, "다시 써주세요"), client=client
    )

    await _click(handlers_with_revise, sms.APPROVE, approve_body, client)


async def test_수정_콜백이_없어도_카드는_정리된다(client, handlers):
    # revise 를 주입하지 않은 곳(테스트·다른 봇)에서도 버튼이 죽지 않아야 한다.
    await _draft(client)
    await _click(handlers, sms.REVISE, _press(client, sms.REVISE), client)

    await handlers[sms.REVISE_VIEW](
        ack=_ack, body=_submit(client, "고쳐주세요"), client=client
    )

    assert "고쳐주세요" in client.updated[-1]["text"]
    assert not sms._DRAFTS


async def test_예약_초안을_수정할_때도_예약이_보인다(client, handlers_with_revise):
    # 언제 나가는 건지 모르고 피드백을 적으면, 마감 문구를 고치면서
    # 발송 시각과 어긋난 안내를 쓰게 된다.
    await _draft(client, send_at=_later(60))

    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)

    assert "KST" in str(client.views[-1]["blocks"])


async def test_수정_모달은_받는_사람도_보여준다(client, handlers_with_revise):
    # 명단에서 한 명 빼는 것도 피드백으로 되는데, 문안만 떠 있으면 고칠 수
    # 있는 줄 모르고 취소한 뒤 처음부터 다시 말하게 된다.
    await _draft(client)

    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)

    modal = str(client.views[-1]["blocks"])
    # 존재 여부만 보면 짝이 뒤바뀌어도 통과한다. 지목하려면 짝이 맞아야 한다.
    assert "01011111111  가" in modal
    assert "01022222222  나" in modal


async def test_수정_모달이_슬랙_한도를_넘지_않는다(client, handlers_with_revise):
    # 넘으면 슬랙이 거절해 모달이 아예 안 열린다 — [수정] 이 죽은 버튼이 된다.
    targets = [
        {
            "to": f"010-1111-{i:04d}",
            "name": f"이름{i}",
            "var1": "https://x/" + "a" * 300,
        }
        for i in range(1, 31)
    ]
    await _draft(client, targets=targets, content="[*이름*] [*1*]")

    await _click(handlers_with_revise, sms.REVISE, _press(client, sms.REVISE), client)

    for block in client.views[-1]["blocks"]:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000


async def test_같은_초안을_두_번_올리지_않는다(client):
    # 카드를 올린 뒤 코드가 터지면 도구는 "실행 실패" 만 돌려준다. 모델은
    # 아무 일도 없었던 줄 알고 통째로 다시 내고, 두 장 다 눌리면 같은
    # 사람이 문자를 두 번 받는다.
    첫째 = await _draft(client)
    둘째 = await _draft(client)

    assert "초안을 올렸습니다" in 첫째
    assert "이미 이 스레드에" in 둘째
    assert len(client.posted) == 1


async def test_치환값이_다르면_새_초안을_올린다(client):
    # 문안 틀과 번호가 같아도 기수가 다르면 다른 발송이다. 접어 버리면
    # 값이 틀린 첫 카드를 누르라고 안내하게 된다.
    행 = {"to": "010-1111-2222", "name": "가", "var1": "1기"}

    await _draft(client, targets=[행], content="[*이름*] [*1*]")
    await _draft(client, targets=[{**행, "var1": "2기"}], content="[*이름*] [*1*]")

    assert len(client.posted) == 2


async def test_번호_순서만_다르면_같은_초안이다(client):
    # 모델이 코드를 고칠 때 정렬이나 groupby 가 바뀌면 순서가 달라진다.
    # 그것을 다른 발송으로 보면 중복 검사를 그냥 통과한다.
    둘 = [{"to": "010-1111-2222", "name": "가"}, {"to": "010-3333-4444", "name": "나"}]

    await _draft(client, targets=둘)
    await _draft(client, targets=list(reversed(둘)))

    assert len(client.posted) == 1


async def test_카드를_못_올리면_초안이_남지_않는다(client):
    # 앞서 저장하면 전송이 실패했을 때 카드는 없는데 지문만 남아, 이후
    # 재시도가 전부 "그 카드에서 보내기를 누르세요" 로 거절된다.
    async def 실패(**kwargs):
        raise RuntimeError("ratelimited")

    client.chat_postMessage = 실패
    with pytest.raises(RuntimeError):
        await _draft(client)

    assert len(sms._DRAFTS) == 0


async def test_코드가_부른_초안이_카드로_올라간다(client):
    # general.py 가 .coroutine 을 샌드박스에 넘긴다. 그 이음매를 진짜
    # 객체로 걷는다 -- 속성이 사라지거나 to_sync 가 깨지면 여기서 죽는다.
    도구 = _code_tool(client)

    결과 = await 도구.ainvoke(
        {
            "code": (
                "print(draft_sms(content='[*이름*]님',"
                " targets=[{'to': '010-1111-2222', 'name': '가'}]))"
            )
        }
    )

    assert "초안을 올렸습니다" in 결과
    assert len(client.posted) == 1


async def test_다른_스레드면_새_초안을_올린다(client):
    # _DRAFTS 는 프로세스 전역이고 봇 넷이 한 프로세스를 쓴다. 채널과
    # 스레드를 안 보면 남의 스레드 초안이 내 카드를 막는다. 그때 도구가
    # 가리키는 카드는 볼 수도 없는 곳에 있다.
    await _draft(client)

    다른곳 = sms.get_draft_sms_tool(client, "C2", "999.000")
    await 다른곳.ainvoke({"content": "[*이름*]님", "targets": ROWS, "send_at": ""})

    assert len(client.posted) == 2


async def test_초안_결과는_print_하지_않아도_돌아온다(client):
    # draft_sms 는 실패를 문자열로 돌려준다. 코드가 반환을 안 받으면 그것이
    # 통째로 사라지고 도구는 "성공" 만 답한다. 카드는 0장인데 모델은 올라간
    # 줄 알고 사람에게 그렇게 말한다.
    도구 = _code_tool(client)

    결과 = await 도구.ainvoke(
        {
            "code": (
                "draft_sms(content='[*이름*]님',"
                " targets=[{'to': '없는번호', 'name': '가'}])"
            )
        }
    )

    assert len(client.posted) == 0
    assert "보내기 전에 고칠 것" in 결과


async def test_같은_턴에_두_번_실려도_카드는_한_장이다(client):
    # ToolNode 가 한 AI 메시지의 tool call 을 gather 로 돌린다. 검사와 저장
    # 사이에 await 가 있으면 둘 다 빠져나가 카드가 두 장 된다.
    보내기 = client.chat_postMessage

    async def 네트워크처럼(**kwargs):
        # FakeClient 는 await 지점이 없어 루프를 놓지 않는다. 실제 슬랙은
        # 네트워크에서 놓으므로, 그 자리를 만들지 않으면 창이 안 열린다.
        await asyncio.sleep(0)
        return await 보내기(**kwargs)

    client.chat_postMessage = 네트워크처럼
    도구 = sms.get_draft_sms_tool(client, "C1", "111.000")
    인자 = {"content": "[*이름*]님", "targets": ROWS, "send_at": ""}

    답 = await asyncio.gather(도구.ainvoke(인자), 도구.ainvoke(인자))

    assert len(client.posted) == 1
    assert sum("이미 이 스레드에" in 하나 for 하나 in 답) == 1


async def test_반복_응답이_집계_결과를_밀어내지_않는다(client):
    # 거절 응답은 글자 하나 다르지 않다. 접지 않으면 상한을 채워 모델이
    # 방금 계산한 것을 통째로 밀어낸다.
    도구 = _code_tool(client)

    결과 = await 도구.ainvoke(
        {
            "code": (
                "print('중요한집계결과=42')\n"
                "for _ in range(3):\n"
                "    draft_sms(content='[*이름*]님',"
                " targets=[{'to': '010-1111-2222', 'name': '가'}])\n"
            )
        }
    )

    assert "중요한집계결과=42" in 결과
    assert 결과.count("초안을 올렸습니다") == 1
    assert 결과.count("이미 이 스레드에") == 1


async def test_카드가_여러_장이면_모델도_여러_장으로_본다(client):
    # 받는 사람이 달라도 인원이 같으면 draft_sms 의 답이 글자 하나 다르지
    # 않다. 그냥 접으면 카드 다섯 장이 한 줄이 되고, 모델은 "그 카드" 라고
    # 단수로 안내한다. 사람은 한 장만 누르고 나머지는 안 나간다.
    도구 = _code_tool(client)

    결과 = await 도구.ainvoke(
        {
            "code": (
                "for i in range(3):\n"
                "    draft_sms(content='[*이름*]님',"
                " targets=[{'to': f'010-1111-{i:04d}', 'name': '가'}])\n"
            )
        }
    )

    assert len(client.posted) == 3
    assert "(×3)" in 결과


async def test_거절된_호출은_카드_예산을_쓰지_않는다(client):
    # 번호가 깨진 호출은 슬랙을 건드리지 않는다. 그것이 예산을 먹으면
    # 카드 0장인 채로 멀쩡한 명단이 거절된다.
    도구 = _code_tool(client)

    결과 = await 도구.ainvoke(
        {
            "code": (
                "for _ in range(6):\n"
                "    draft_sms(content='[*이름*]님',"
                " targets=[{'to': '없는번호', 'name': '가'}])\n"
                "draft_sms(content='[*이름*]님',"
                " targets=[{'to': '010-1111-2222', 'name': '가'}])\n"
            )
        }
    )

    assert len(client.posted) == 1
    assert "초안을 올렸습니다" in 결과


async def test_카드_상한에_걸려도_코드는_계속_돈다(client):
    # 예외로 끊으면 이미 올라간 카드는 그대로인데 도구는 "실행 실패" 만
    # 답한다. 모델은 카드가 없는 줄 알고, 사람 눈에 보이는 것과 어긋난다.
    도구 = _code_tool(client)

    결과 = await 도구.ainvoke(
        {
            "code": (
                "for i in range(8):\n"
                "    draft_sms(content='[*이름*]님',"
                " targets=[{'to': f'010-2222-{i:04d}', 'name': '가'}])\n"
                "print('끝까지왔다')\n"
            )
        }
    )

    assert len(client.posted) == python_tools.DRAFT_CARDS_PER_RUN
    assert "끝까지왔다" in 결과
    assert "더 올리지 않았습니다" in 결과


async def test_예약_초안도_카드_예산을_쓴다(client):
    # 예약 갈래는 문구가 따로다. 그 문구에서 POSTED_MARK 가 빠지면 카드는
    # 올라가는데 예산을 안 써서 상한이 영영 안 걸린다.
    tool = _code_tool(client)

    result = await tool.ainvoke(
        {
            "code": (
                f"for i in range(8):\n"
                f"    draft_sms(content='[*이름*]님',"
                f" targets=[{{'to': f'010-3333-{{i:04d}}', 'name': '가'}}],"
                f" send_at='{_later(30)}')\n"
            )
        }
    )

    assert len(client.posted) == python_tools.DRAFT_CARDS_PER_RUN
    assert "더 올리지 않았습니다" in result


async def test_카드를_안_올리는_호출도_무한히_반복할_수_없다(client):
    # 거절과 형식 오류는 카드 예산을 안 쓴다. 그것만 보면 상한이 영영 안
    # 걸린다. preview 가 동기라 루프 위에서 도는데 봇 넷이 그 루프를 나눠 쓴다.
    tool = _code_tool(client)

    result = await tool.ainvoke(
        {
            "code": (
                "for i in range(60):\n"
                "    draft_sms(content='[*이름*]님',"
                " targets=[{'to': '없는번호', 'name': '가'}])\n"
            )
        }
    )

    assert len(client.posted) == 0
    assert "상한이라 더 올리지 않았습니다" in result
