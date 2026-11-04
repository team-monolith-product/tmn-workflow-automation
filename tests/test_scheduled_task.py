"""로봇 작업 예약의 시각 규칙, 도구, 실행 흐름 테스트

표는 tests/test_scheduled_task_db.py 가 실제 Postgres 로 확인합니다. 여기서는
store 의 저장 함수를 가짜로 바꿔 끼웁니다.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import scheduled_task
from service.scheduled_task import (
    KST,
    Claim,
    Schedule,
    ScheduledTask,
    ScheduleProblem,
    resolve_schedule,
)

# 수요일 오전 10시
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=KST)


def _task(**overrides) -> ScheduledTask:
    values = {
        "id": 7,
        "channel_id": "C1",
        "thread_ts": "111.222",
        "created_by": "U1",
        "prompt": "어제 가입자 수 알려줘",
        "run_at": NOW,
        "cron": None,
        "until": None,
    }
    return ScheduledTask(**{**values, **overrides})


def _tools(channel="C1", user="U1") -> dict:
    tools = scheduled_task.get_scheduled_task_tools(channel, "111.222", user)
    return {t.name: t for t in tools}


def test_한_번짜리는_시간대_없으면_KST_로_읽고_상대_시각은_지금부터_더한다():
    # When : 시간대 없는 절대 시각과 30분 뒤로 정하면
    absolute = resolve_schedule("2026-11-05 09:00", None, None, None, NOW)
    relative = resolve_schedule(None, 30, None, None, NOW)

    # Then : 반복 없이 KST 시각과 지금+30분이 된다
    assert absolute == Schedule(datetime(2026, 11, 5, 9, 0, tzinfo=KST))
    assert relative == Schedule(NOW + timedelta(minutes=30))


def test_반복은_다음_해당_시각부터_until_날짜_끝까지다():
    # When : 매주 수요일 16시, 1월 31일까지로 정하면
    schedule = resolve_schedule(None, None, "0 16 * * wed", "2027-01-31", NOW)

    # Then : 첫 회차는 오늘(수) 16시, 끝은 1월 31일 23:59:59
    assert schedule == Schedule(
        datetime(2026, 11, 4, 16, 0, tzinfo=KST),
        "0 16 * * wed",
        datetime(2027, 1, 31, 23, 59, 59, tzinfo=KST),
    )


@pytest.mark.parametrize(
    "args, message",
    [
        (("2026-11-04 09:59", None, None, None), "지났습니다"),
        (("2026-11-05 09:00", 30, None, None), "하나만"),
        (("2026-11-05 09:00", None, "0 16 * * wed", None), "하나만"),
        ((None, None, "0 16 * * 3", None), "이름"),
        ((None, None, "* * * * *", None), "간격"),
        ((None, 30, None, "2027-01-31"), "repeat 와 함께"),
    ],
)
def test_잘못된_시각_인자는_지금_시각을_붙인_안내로_거절한다(args, message):
    # When : 지난 시각, 겹친 인자, 요일 숫자, 매분, 반복 없는 until 을 주면
    with pytest.raises(ScheduleProblem) as problem:
        resolve_schedule(*args, NOW)

    # Then : 고칠 점과 지금 시각을 함께 알려준다
    assert message in str(problem.value)
    assert "지금은 2026-11-04" in str(problem.value)


def test_사람이_묻지_않은_질문에는_예약_도구를_주지_않는다():
    # When : 워크플로우 메시지처럼 질문한 사람이 없으면
    tools = scheduled_task.get_scheduled_task_tools("C1", "111.222", None)

    # Then : 예약은 사람 이름으로 실행되므로 도구가 없다
    assert tools == []


@pytest.mark.asyncio
async def test_예약하면_질문한_스레드와_사람과_정한_시각으로_넣는다(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduled_task.store,
        "create",
        lambda *args: calls.append(args) or _task(cron=args[4].cron),
    )

    # When : 매주 수요일 16시로 예약하면
    result = await _tools()["schedule_task"].ainvoke(
        {"prompt": "회의록 요청", "repeat": "0 16 * * wed"}
    )

    # Then : 이 채널·스레드·사람과 반복 규칙으로 넣고 번호와 주기를 돌려준다
    channel, thread_ts, user, prompt, schedule = calls[0]
    assert (channel, thread_ts, user, prompt) == ("C1", "111.222", "U1", "회의록 요청")
    assert schedule.cron == "0 16 * * wed"
    assert "#7" in result and "반복" in result


@pytest.mark.asyncio
async def test_시각_인자가_잘못되면_넣지_않고_고칠_점을_돌려준다(monkeypatch):
    create = MagicMock()
    monkeypatch.setattr(scheduled_task.store, "create", create)

    # When : 요일을 숫자로 주면
    result = await _tools()["schedule_task"].ainvoke(
        {"prompt": "x", "repeat": "0 16 * * 3"}
    )

    # Then : 저장하지 않고 안내문을 돌려준다
    create.assert_not_called()
    assert "이름" in result


def _client() -> AsyncMock:
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "999.1"}
    return client


def _card_text(call) -> str:
    """카드 블록의 글자를 이어 붙입니다."""
    texts = []
    for block in call.kwargs["blocks"]:
        if block["type"] == "section":
            texts.append(block["text"]["text"])
        else:
            texts.extend(element["text"] for element in block["elements"])
    return "\n".join(texts)


@pytest.mark.asyncio
async def test_시각이_된_회차는_실행_카드를_올리고_에이전트를_돌린_뒤_완료로_고친다(
    monkeypatch,
):
    finished = []
    following = datetime(2026, 11, 11, 16, 0, tzinfo=KST)
    claim = Claim(_task(cron="0 16 * * wed"), following)
    monkeypatch.setattr(scheduled_task.store, "claim_due", lambda now: [claim])
    monkeypatch.setattr(
        scheduled_task.store, "finish", lambda *args: finished.append(args)
    )
    agent = AsyncMock()
    client = _client()

    # When : 폴러가 돌고 백그라운드 실행이 끝나면
    await scheduled_task.run_due_tasks(agent, client)
    await asyncio.gather(*scheduled_task._running)

    # Then : 스레드에 실행 중 카드를 다음 회차와 함께 올리고, 다시 예약하지 말라는
    #        말과 함께 요청을 태우고, 에러 없이 끝낸 뒤 같은 카드를 완료로 고친다
    posted = client.chat_postMessage.await_args
    assert posted.kwargs["thread_ts"] == "111.222"
    assert "실행 중" in _card_text(posted) and "2026-11-11" in _card_text(posted)
    channel, thread_ts, user, text = agent.await_args.args
    assert (channel, thread_ts, user) == ("C1", "111.222", "U1")
    assert "다시 예약하지 말고" in text and text.endswith("어제 가입자 수 알려줘")
    assert finished == [(7, None)]
    updated = client.chat_update.await_args
    assert updated.kwargs["ts"] == "999.1" and "완료" in _card_text(updated)


@pytest.mark.asyncio
async def test_에이전트가_실패하면_이유를_남기고_카드를_실패로_고친다(monkeypatch):
    finished = []
    monkeypatch.setattr(
        scheduled_task.store, "claim_due", lambda now: [Claim(_task(), None)]
    )
    monkeypatch.setattr(
        scheduled_task.store, "finish", lambda *args: finished.append(args)
    )
    agent = AsyncMock(side_effect=RuntimeError("notion down"))
    client = _client()

    # When : 에이전트가 예외를 던지면
    await scheduled_task.run_due_tasks(agent, client)
    await asyncio.gather(*scheduled_task._running)

    # Then : 이유와 함께 끝내고, 따로 글을 올리지 않고 실행 카드를 실패와 이유로 고친다
    assert finished[0][0] == 7 and "notion down" in finished[0][1]
    assert client.chat_postMessage.await_count == 1
    card = _card_text(client.chat_update.await_args)
    assert "실패" in card and "notion down" in card


@pytest.mark.asyncio
async def test_카드조차_못_올리면_에이전트를_돌리지_않고_실패로_남긴다(monkeypatch):
    finished = []
    monkeypatch.setattr(
        scheduled_task.store, "claim_due", lambda now: [Claim(_task(), None)]
    )
    monkeypatch.setattr(
        scheduled_task.store, "finish", lambda *args: finished.append(args)
    )
    agent = AsyncMock()
    client = _client()
    client.chat_postMessage.side_effect = RuntimeError("is_archived")

    # When : 채널이 보관돼 카드를 못 올리면
    await scheduled_task.run_due_tasks(agent, client)
    await asyncio.gather(*scheduled_task._running)

    # Then : 에이전트는 돌지 않고, 이유를 남겨 끝내며, 고칠 카드가 없어 고치지 않는다
    agent.assert_not_awaited()
    assert "is_archived" in finished[0][1]
    client.chat_update.assert_not_awaited()
