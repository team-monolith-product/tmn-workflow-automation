"""
로봇에게 작업을 예약하고 취소하는 흐름입니다.

    ① 슬랙에서 "내일 9시에 어제 가입자 수 뽑아서 알려줘",
       "1월까지 매주 수요일 16시에 @운영 태그해서 회의록 쓰라고 채널에 올려줘" 라고 말한다
    ② 에이전트가 schedule_task 로 예약을 넣는다 (scheduled_task 표)
    ③ 1분마다 도는 run_due_tasks 가 시각이 된 예약을 꺼내, 예약을 건 스레드에
       알림을 올리고 그 요청을 예약한 사람이 물은 것처럼 에이전트에게 맡긴다.
       반복 예약이면 다음 회차 시각으로 옮겨 둔다
    ④ "예약 뭐 있어?", "3번 취소해줘" 는 list_scheduled_tasks·cancel_scheduled_task

회차는 예약을 건 스레드에서 돌아 그 스레드가 실행 기록이 됩니다. 채널에 보이게
올려야 하는 공지는 에이전트가 post_channel_message 로 채널에 새 글로 올립니다.

예약은 걸린 채널 안에서만 보이고 취소됩니다. 실행할 때의 도구도 그 채널과 예약한
사람 기준으로 다시 만듭니다 -- 지금 묻는 것과 똑같이 돈다.
"""

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from langchain_core.tools import StructuredTool, tool
from slack_sdk.web.async_client import AsyncWebClient

from service import scheduled_task as store
from service.scheduled_task import ScheduledTask
from service.slack import escape

from .common import KST

# 스레드에서 에이전트를 돌리는 콜백. app.general.get_thread_agent 가 만든다.
ThreadAgent = Callable[[str, str, str, str], Awaitable[None]]

# 너무 먼 예약은 잊힌 채로 남는다. 그보다 먼 일은 노션 작업으로 남긴다.
MAX_AHEAD = timedelta(days=366)

# 반복 예약의 최소 간격. 크론식을 잘못 써서 매분 채널에 글이 올라가는 것을 막는다.
MIN_INTERVAL = timedelta(hours=1)

# 실행 중인 예약. create_task 참조를 들고 있어야 GC 되지 않는다.
_running: set[asyncio.Task] = set()


def _format(run_at: datetime) -> str:
    """실행 시각을 KST 로 보여줍니다."""
    return run_at.astimezone(KST).strftime("%Y-%m-%d(%a) %H:%M")


def parse_run_at(
    run_at: str | None, in_minutes: int | None, now: datetime
) -> datetime | str:
    """도구 인자에서 실행 시각을 정합니다.

    Args:
        run_at: 절대 시각 문자열. 시간대가 없으면 KST 로 읽습니다.
        in_minutes: 지금부터 몇 분 뒤
        now: 지금 시각 (시간대 포함)

    Returns:
        datetime | str: 실행 시각. 인자가 잘못됐으면 에이전트에게 돌려줄 안내문
    """
    now_text = f"지금은 {_format(now)} KST 입니다."
    if (run_at is None) == (in_minutes is None):
        return f"run_at 과 in_minutes 중 하나만 주세요. {now_text}"
    if in_minutes is not None:
        if in_minutes < 1:
            return f"in_minutes 는 1 이상이어야 합니다. {now_text}"
        when = now + timedelta(minutes=in_minutes)
    else:
        try:
            when = datetime.fromisoformat(run_at)
        except ValueError:
            return (
                f"run_at '{run_at}' 을 읽지 못했습니다."
                f" 'YYYY-MM-DD HH:MM' 으로 주세요. {now_text}"
            )
        if when.tzinfo is None:
            when = when.replace(tzinfo=KST)
    if when <= now:
        return f"{_format(when)} 은 이미 지났습니다. {now_text}"
    if when - now > MAX_AHEAD:
        return f"1년 넘게 뒤로는 예약하지 않습니다. {now_text}"
    return when


def next_fire(cron: str, after: datetime) -> datetime:
    """크론식의 after 뒤 첫 시각을 돌려줍니다. after 와 같은 시각은 넣지 않습니다.

    Args:
        cron: KST 5필드 크론식
        after: 기준 시각 (시간대 포함)

    Returns:
        datetime: 다음 시각
    """
    trigger = CronTrigger.from_crontab(cron, timezone=KST)
    return trigger.get_next_fire_time(None, after + timedelta(seconds=1))


def parse_repeat(
    repeat: str, until: str | None, now: datetime
) -> tuple[datetime, datetime | None] | str:
    """반복 예약 인자에서 첫 실행 시각과 끝 시각을 정합니다.

    Args:
        repeat: KST 5필드 크론식
        until: 반복을 멈출 날짜나 시각. 날짜만 주면 그날 끝까지. 없으면 None
        now: 지금 시각 (시간대 포함)

    Returns:
        tuple[datetime, datetime | None] | str: (첫 실행 시각, 끝 시각).
            인자가 잘못됐으면 에이전트에게 돌려줄 안내문
    """
    now_text = f"지금은 {_format(now)} KST 입니다."
    fields = repeat.split()
    # APScheduler 는 요일 숫자를 0=월요일로 읽는다. 크론식의 0=일요일과 하루씩
    # 어긋나므로 숫자를 받지 않는다.
    if len(fields) == 5 and any(c.isdigit() for c in fields[4]):
        return "요일은 숫자 대신 mon,tue,wed,thu,fri,sat,sun 이름으로 주세요."
    try:
        first = next_fire(repeat, now)
    except ValueError as error:
        return (
            f"repeat '{repeat}' 을 읽지 못했습니다({error})."
            " '분 시 일 월 요일' 5필드 크론식으로 주세요. 예: 매주 수요일 16시 '0 16 * * wed'"
        )
    if next_fire(repeat, first) - first < MIN_INTERVAL:
        return f"반복 간격은 {MIN_INTERVAL} 이상이어야 합니다."
    end = None
    if until is not None:
        try:
            end = datetime.fromisoformat(until)
        except ValueError:
            return f"until '{until}' 을 읽지 못했습니다. 'YYYY-MM-DD' 로 주세요."
        if len(until) == 10:
            end = end.replace(hour=23, minute=59, second=59)
        if end.tzinfo is None:
            end = end.replace(tzinfo=KST)
        if end < first:
            return (
                f"until {_format(end)} 전에는 한 번도 돌지 않습니다"
                f"(첫 회차 {_format(first)}). {now_text}"
            )
    return first, end


def _describe(task: ScheduledTask) -> str:
    """예약 한 줄 요약. 반복이면 주기와 끝을 붙입니다."""
    text = f"#{task.id} 다음 {_format(task.run_at)} KST"
    if task.cron:
        text += f" · 반복 `{task.cron}`"
        if task.until:
            text += f" ~{_format(task.until)}"
    return text


def get_scheduled_task_tools(
    client: AsyncWebClient, channel: str, thread_ts: str, user_id: str | None
) -> list[StructuredTool]:
    """작업 예약·조회·취소 도구와 채널 새 글 도구를 돌려줍니다.

    예약은 사람 이름으로 실행되므로 사람이 건 질문에서만 줍니다.

    Args:
        client: 슬랙 클라이언트
        channel: 채널 ID
        thread_ts: 결과를 올릴 스레드
        user_id: 질문한 사람. 워크플로우 메시지면 None

    Returns:
        list[StructuredTool]: [예약, 조회, 취소, 채널 새 글] 또는 []
    """
    if user_id is None:
        return []

    @tool
    async def schedule_task(
        prompt: str,
        run_at: str | None = None,
        in_minutes: int | None = None,
        repeat: str | None = None,
        until: str | None = None,
    ) -> str:
        """
        정해진 시각에 로봇이 할 일을 예약합니다. 시각이 되면 이 스레드에서
        prompt 를 지금 질문한 사람이 물은 것처럼 로봇이 수행합니다.

        prompt 는 그 시각에 바로 할 일 자체만 씁니다. "내일 9시에", "매주" 같은
        시각 표현은 빼고, 나중에 스레드를 처음 보는 사람도 알 수 있게 완결된
        문장으로 씁니다. 사람·그룹 멘션은 대화에 보이는 슬랙 원문 형식
        (<@U...>, <!subteam^S...>) 그대로 남깁니다. 링크·이름처럼 그때 필요한
        정보도 빠짐없이 넣습니다.
        결과를 채널에 공지처럼 보이게 올려야 하면 prompt 에 "채널에 새 글로 올려줘"
        를 넣습니다. 아니면 결과는 이 스레드에 답글로 남습니다.
        예: "어제 하루 신규 가입 교사 수를 Athena 로 집계해 알려줘"
        예: "<!subteam^S09FZSEARJQ> 를 태그해 '이번 달 N주차 회의록 작성해 주세요' 와
            이번 달 회의록 링크(11월 https://..., 12월 https://...)를 채널에 새 글로 올려줘"

        시각은 셋 중 하나로 줍니다.
        - run_at: 한 번, KST 절대 시각 'YYYY-MM-DD HH:MM'
        - in_minutes: 한 번, 지금부터 몇 분 뒤 ("30분 뒤", "2시간 뒤")
        - repeat: 반복, KST 5필드 크론식 '분 시 일 월 요일'.
          매주 수 16시 '0 16 * * wed', 평일 9시 '0 9 * * mon-fri', 매월 1일 10시 '0 10 1 * *'.
          until 에 끝 날짜 'YYYY-MM-DD' 를 주면 그날까지만 돕니다. 첫 회차는 다음 해당 시각입니다.
        1분 단위로 확인하므로 최대 1분쯤 늦을 수 있습니다.

        Returns:
            예약 번호와 다음 실행 시각, 또는 고칠 점
        """
        now = datetime.now(KST)
        cron = None
        end = None
        if repeat is not None:
            if run_at is not None or in_minutes is not None:
                return "repeat 를 주면 run_at·in_minutes 는 비웁니다. 첫 회차는 크론식이 정합니다."
            parsed = parse_repeat(repeat, until, now)
            if isinstance(parsed, str):
                return parsed
            when, end = parsed
            cron = repeat
        else:
            if until is not None:
                return "until 은 repeat 와 함께만 씁니다."
            when = parse_run_at(run_at, in_minutes, now)
            if isinstance(when, str):
                return when
        task = await asyncio.to_thread(
            store.create, channel, thread_ts, user_id, prompt, when, cron, end
        )
        if task is None:
            return (
                f"이 채널에 대기 중인 예약이 {store.MAX_PENDING_PER_CHANNEL}개라"
                " 더 걸 수 없습니다. 필요 없는 예약을 먼저 취소해 주세요."
            )
        return f"예약했습니다: {_describe(task)}"

    @tool
    async def list_scheduled_tasks() -> str:
        """
        이 채널에 걸린, 아직 실행되지 않은 예약을 보여줍니다.

        Returns:
            예약 번호·실행 시각·예약한 사람·할 일 목록
        """
        tasks = await asyncio.to_thread(store.list_pending, channel)
        if not tasks:
            return "이 채널에 대기 중인 예약이 없습니다."
        return "\n".join(f"{_describe(t)} <@{t.created_by}>: {t.prompt}" for t in tasks)

    @tool
    async def cancel_scheduled_task(task_id: int) -> str:
        """
        이 채널에 걸린 예약을 취소합니다. 번호를 모르면 list_scheduled_tasks 로
        먼저 찾습니다. 이미 실행됐거나 다른 채널의 예약은 취소할 수 없습니다.

        Args:
            task_id: 예약 번호 (#3 이면 3)

        Returns:
            취소 결과
        """
        canceled = await asyncio.to_thread(store.cancel, task_id, channel)
        if canceled:
            return f"예약 #{task_id} 를 취소했습니다."
        return (
            f"예약 #{task_id} 를 취소하지 못했습니다."
            " 이 채널에 대기 중인 예약이 아닙니다(없거나, 이미 실행됐거나, 다른 채널)."
        )

    @tool
    async def post_channel_message(text: str) -> str:
        """
        이 채널에 스레드 답글이 아니라 새 글로 메시지를 올립니다. 공지처럼
        채널에서 바로 보여야 할 때만 씁니다 -- 사용자나 예약 요청이 채널에
        올리라고 했을 때. 보통 답변은 이 도구 없이 그냥 답하면 스레드에 남습니다.

        사람·그룹 멘션은 슬랙 원문 형식(<@U...>, <!subteam^S...>)으로 씁니다.

        Args:
            text: 올릴 메시지. 슬랙 mrkdwn

        Returns:
            올린 글의 링크
        """
        posted = await client.chat_postMessage(channel=channel, text=text)
        link = await client.chat_getPermalink(channel=channel, message_ts=posted["ts"])
        return f"채널에 올렸습니다: {link['permalink']}"

    return [
        schedule_task,
        list_scheduled_tasks,
        cancel_scheduled_task,
        post_channel_message,
    ]


async def _run(task: ScheduledTask, agent: ThreadAgent, client: AsyncWebClient) -> None:
    """예약 한 회차를 실행하고 결과를 표에 남깁니다.

    백그라운드 태스크라 예외를 여기서 잡지 않으면 아무도 모르게 사라지고 예약은
    running 에 남습니다. 잡아서 기록하고 스레드에 알립니다. 반복 예약은 실패해도
    다음 회차로 옮깁니다.
    """
    next_run_at = None
    if task.cron:
        after = max(datetime.now(KST), task.run_at)
        next_run_at = next_fire(task.cron, after)
        if task.until and next_run_at > task.until:
            next_run_at = None
    next_text = (
        f"다음 회차는 {_format(next_run_at)} 입니다."
        if next_run_at
        else ("마지막 회차입니다." if task.cron else "")
    )
    try:
        await client.chat_postMessage(
            channel=task.channel_id,
            thread_ts=task.thread_ts,
            text=(
                f":alarm_clock: 예약 #{task.id} 실행"
                f" (<@{task.created_by}> 님 예약) {next_text}\n"
                f"> {escape(task.prompt)}"
            ),
        )
        # 예약할 때의 "매주 해줘" 가 스레드에 남아 있어 에이전트가 또 예약하려
        # 들 수 있다. 지금이 그 시각이라고 못 박는다.
        await agent(
            task.channel_id,
            task.thread_ts,
            task.created_by,
            f"[예약 #{task.id} 실행] 지금이 예약한 시각입니다."
            f" 다시 예약하지 말고 바로 수행하세요.\n{task.prompt}",
        )
    except Exception as error:
        traceback.print_exc()
        await asyncio.to_thread(store.finish, task.id, repr(error), next_run_at)
        await client.chat_postMessage(
            channel=task.channel_id,
            thread_ts=task.thread_ts,
            text=f":x: 예약 #{task.id} 실행에 실패했습니다. {next_text}\n```{error}```",
        )
        return
    await asyncio.to_thread(store.finish, task.id, None, next_run_at)


async def run_due_tasks(agent: ThreadAgent, client: AsyncWebClient) -> None:
    """시각이 된 예약을 맡아 각각 백그라운드로 실행합니다.

    스케줄러가 1분마다 부릅니다. 에이전트는 몇 분씩 걸리므로 기다리지 않고 바로
    돌아옵니다 -- 기다리면 다음 확인이 밀려 뒤 예약이 늦어집니다.

    Args:
        agent: 스레드에서 에이전트를 돌리는 콜백
        client: 슬랙 클라이언트
    """
    for task in await asyncio.to_thread(store.claim_due):
        print(f"[scheduled_task] #{task.id} 실행 시작")
        job = asyncio.create_task(_run(task, agent, client))
        _running.add(job)
        job.add_done_callback(_running.discard)
