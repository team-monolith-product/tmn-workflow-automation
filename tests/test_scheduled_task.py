"""로봇 작업 예약의 도구와 실행 흐름 테스트

표는 tests/test_scheduled_task_db.py 가 실제 Postgres 로 확인합니다. 여기서는
store 를 메모리 가짜로 바꿔 끼웁니다.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app import scheduled_task
from app.common import KST
from service.scheduled_task import ScheduledTask

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
    tools = scheduled_task.get_scheduled_task_tools(
        AsyncMock(), channel, "111.222", user
    )
    return {t.name: t for t in tools}


def test_시간대_없는_시각은_KST_로_읽고_지난_시각과_둘_다_준_인자는_거절한다():
    # When : 시간대 없는 절대 시각, 분 단위 상대 시각, 지난 시각, 둘 다 준 경우
    absolute = scheduled_task.parse_run_at("2026-11-05 09:00", None, NOW)
    relative = scheduled_task.parse_run_at(None, 30, NOW)
    past = scheduled_task.parse_run_at("2026-11-04 09:59", None, NOW)
    both = scheduled_task.parse_run_at("2026-11-05 09:00", 30, NOW)

    # Then : 절대 시각은 KST 로, 상대 시각은 지금부터 더한 값으로, 나머지는 안내문으로
    assert absolute == datetime(2026, 11, 5, 9, 0, tzinfo=KST)
    assert relative == NOW + timedelta(minutes=30)
    assert isinstance(past, str) and "지났습니다" in past
    assert isinstance(both, str) and "하나만" in both


def test_사람이_묻지_않은_질문에는_예약_도구를_주지_않는다():
    # When : 워크플로우 메시지처럼 질문한 사람이 없으면
    tools = scheduled_task.get_scheduled_task_tools(AsyncMock(), "C1", "111.222", None)

    # Then : 예약은 사람 이름으로 실행되므로 도구가 없다
    assert tools == []


@pytest.mark.asyncio
async def test_예약하면_질문한_스레드와_사람으로_넣는다(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduled_task.store,
        "create",
        lambda *args: calls.append(args) or _task(run_at=args[4]),
    )

    # When : 30분 뒤로 예약하면
    result = await _tools()["schedule_task"].ainvoke(
        {"prompt": "어제 가입자 수 알려줘", "in_minutes": 30}
    )

    # Then : 이 채널·스레드·사람으로 넣고 예약 번호를 돌려준다
    channel, thread_ts, user, prompt, _, cron, until = calls[0]
    assert (channel, thread_ts, user, prompt) == (
        "C1",
        "111.222",
        "U1",
        "어제 가입자 수 알려줘",
    )
    assert (cron, until) == (None, None)
    assert "#7" in result


@pytest.mark.asyncio
async def test_채널의_대기_예약이_한도면_넣지_않았다고_알린다(monkeypatch):
    monkeypatch.setattr(scheduled_task.store, "create", lambda *args: None)

    # When : 한도가 차서 store 가 넣지 않으면
    result = await _tools()["schedule_task"].ainvoke({"prompt": "x", "in_minutes": 5})

    # Then : 취소하라고 안내한다
    assert "취소" in result and "#" not in result


@pytest.mark.asyncio
async def test_취소는_이_채널_기준으로_요청하고_실패를_알린다(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduled_task.store,
        "cancel",
        lambda task_id, channel: calls.append((task_id, channel)) or False,
    )

    # When : 다른 채널의 예약이라 store 가 취소하지 못하면
    result = await _tools(channel="C2")["cancel_scheduled_task"].ainvoke({"task_id": 7})

    # Then : 지금 채널로 요청했고, 취소하지 못했다고 알린다
    assert calls == [(7, "C2")]
    assert "취소하지 못했습니다" in result


@pytest.mark.asyncio
async def test_시각이_된_예약은_스레드에_알리고_예약한_사람으로_에이전트를_돌린다(
    monkeypatch,
):
    finished = []
    monkeypatch.setattr(scheduled_task.store, "claim_due", lambda: [_task()])
    monkeypatch.setattr(
        scheduled_task.store, "finish", lambda *args: finished.append(args)
    )
    agent = AsyncMock()
    client = AsyncMock()

    # When : 폴러가 돌고 백그라운드 실행이 끝나면
    await scheduled_task.run_due_tasks(agent, client)
    await asyncio.gather(*scheduled_task._running)

    # Then : 스레드에 알리고, 다시 예약하지 말라는 말과 함께 요청을 태우고, done 으로 남긴다
    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "111.222"
    channel, thread_ts, user, text = agent.await_args.args
    assert (channel, thread_ts, user) == ("C1", "111.222", "U1")
    assert "다시 예약하지 말고" in text and text.endswith("어제 가입자 수 알려줘")
    assert finished == [(7, None, None)]


@pytest.mark.asyncio
async def test_반복_예약은_실패해도_다음_회차로_옮기고_스레드에_알린다(monkeypatch):
    finished = []
    monkeypatch.setattr(
        scheduled_task.store,
        "claim_due",
        lambda: [_task(cron="0 16 * * wed")],
    )
    monkeypatch.setattr(
        scheduled_task.store, "finish", lambda *args: finished.append(args)
    )
    agent = AsyncMock(side_effect=RuntimeError("notion down"))
    client = AsyncMock()

    # When : 매주 수요일 예약이 실행 중에 예외를 던지면
    await scheduled_task.run_due_tasks(agent, client)
    await asyncio.gather(*scheduled_task._running)

    # Then : 이유를 남기고 지금 뒤 첫 수요일 16시로 옮기며, 실패를 스레드에 올린다
    task_id, error, next_run_at = finished[0]
    assert task_id == 7 and "notion down" in error
    assert next_run_at > datetime.now(KST)
    assert (next_run_at.weekday(), next_run_at.hour) == (2, 16)
    assert "실패" in client.chat_postMessage.await_args.kwargs["text"]


def test_반복은_요일_숫자와_너무_잦은_주기를_거절하고_until_날짜는_그날_끝까지다():
    # When : 요일 숫자, 매분, 이름 요일 + 끝 날짜로 반복을 주면
    numeric = scheduled_task.parse_repeat("0 16 * * 3", None, NOW)
    every_minute = scheduled_task.parse_repeat("* * * * *", None, NOW)
    weekly = scheduled_task.parse_repeat("0 16 * * wed", "2027-01-31", NOW)

    # Then : 앞의 둘은 안내문, 마지막은 오늘(수) 16시부터 1월 31일 끝까지
    assert isinstance(numeric, str) and "이름" in numeric
    assert isinstance(every_minute, str) and "간격" in every_minute
    assert weekly == (
        datetime(2026, 11, 4, 16, 0, tzinfo=KST),
        datetime(2027, 1, 31, 23, 59, 59, tzinfo=KST),
    )


@pytest.mark.asyncio
async def test_반복_예약은_크론식과_끝을_넣고_첫_회차를_크론식이_정한다(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduled_task.store,
        "create",
        lambda *args: calls.append(args) or _task(run_at=args[4], cron=args[5]),
    )

    # When : 매주 수요일 16시, 1월 말까지로 예약하면
    await _tools()["schedule_task"].ainvoke(
        {"prompt": "회의록 요청", "repeat": "0 16 * * wed", "until": "2027-01-31"}
    )

    # Then : 크론식과 끝 시각이 함께 들어가고 첫 회차는 수요일 16시다
    _, _, _, _, run_at, cron, until = calls[0]
    assert cron == "0 16 * * wed"
    assert (run_at.weekday(), run_at.hour) == (2, 16)
    assert until == datetime(2027, 1, 31, 23, 59, 59, tzinfo=KST)
