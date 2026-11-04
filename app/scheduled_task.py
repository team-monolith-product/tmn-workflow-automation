"""
로봇에게 작업을 예약하고 취소하는 흐름입니다.

    ① 슬랙에서 "내일 9시에 어제 가입자 수 뽑아서 알려줘",
       "1월까지 매주 수요일 16시에 @운영 태그해서 회의록 쓰라고 채널에 올려줘" 라고 말한다
    ② 에이전트가 schedule_task 로 예약을 넣는다 (scheduled_task 표)
    ③ 1분마다 도는 run_due_tasks 가 시각이 된 회차를 꺼내, 예약을 건 스레드에
       실행 카드를 올리고 그 요청을 예약한 사람이 물은 것처럼 에이전트에게 맡긴다.
       끝나면 카드를 완료·실패로 고쳐 쓴다
    ④ "예약 뭐 있어?", "3번 취소해줘" 는 list_scheduled_tasks·cancel_scheduled_task

회차는 예약을 건 스레드에서 돌아 그 스레드가 실행 기록이 됩니다. 실행할 때의
도구도 그 채널과 예약한 사람 기준으로 다시 만듭니다 -- 지금 묻는 것과 똑같이
돌아서, 채널 공지나 노션 페이지 만들기처럼 평소에 시킬 수 있는 일은 다 됩니다.

언제 도는지의 규칙과 저장은 service/scheduled_task.py 에 있습니다.
"""

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime

from langchain_core.tools import StructuredTool, tool
from slack_sdk.web.async_client import AsyncWebClient

from service import scheduled_task as store
from service.scheduled_task import (
    KST,
    Claim,
    ScheduledTask,
    ScheduleProblem,
    format_time,
)
from service.slack import escape

# 스레드에서 에이전트를 돌리는 콜백 (channel, thread_ts, user, text).
# app.general.get_draft_revise 가 만든다.
ThreadAgent = Callable[[str, str, str, str], Awaitable[None]]

# 실행 중인 회차. create_task 참조를 들고 있어야 GC 되지 않는다.
_running: set[asyncio.Task] = set()


def _describe(task: ScheduledTask) -> str:
    """예약 한 줄 요약. 반복이면 주기와 끝을 붙입니다."""
    text = f"#{task.id} 다음 {format_time(task.run_at)} KST"
    if task.cron:
        text += f" · 반복 `{task.cron}`"
        if task.until:
            text += f" ~{format_time(task.until)}"
    return text


def get_scheduled_task_tools(
    channel: str, thread_ts: str, user_id: str | None
) -> list[StructuredTool]:
    """작업 예약·조회·취소 도구를 돌려줍니다.

    예약은 사람 이름으로 실행되므로 사람이 건 질문에서만 줍니다.

    Args:
        channel: 채널 ID
        thread_ts: 결과를 올릴 스레드
        user_id: 질문한 사람. 워크플로우 메시지면 None

    Returns:
        list[StructuredTool]: [예약, 조회, 취소] 또는 []
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
        prompt 를 지금 질문한 사람이 물은 것처럼 로봇이 수행합니다. 평소에
        로봇에게 시킬 수 있는 일(노션 페이지 찾기·만들기, 채널 공지, 데이터 집계 등)은
        여러 단계라도 prompt 하나에 담아 시킬 수 있습니다.

        prompt 는 그 시각에 바로 할 일 자체만 씁니다. "내일 9시에", "매주" 같은
        시각 표현은 빼고, 나중에 스레드를 처음 보는 사람도 알 수 있게 완결된
        문장으로 씁니다. 사람·그룹 멘션은 대화에 보이는 슬랙 원문 형식
        (<@U...>, <!subteam^S...>) 그대로 남깁니다. 링크·이름처럼 그때 필요한
        정보도 빠짐없이 넣습니다.
        결과를 채널에 공지처럼 보이게 올려야 하면 prompt 에 "채널에 새 글로 올려줘"
        를 넣습니다. 아니면 결과는 이 스레드에 답글로 남습니다.
        예: "어제 하루 신규 가입 교사 수를 Athena 로 집계해 알려줘"
        예: "노션 회의록 DB 에서 이번 달 운영팀 회의록('YY.MM월 회의록')을 찾고, 없으면
            지난달 회의록을 본떠 만든 뒤, <!subteam^S09FZSEARJQ> 를 태그해 '이번 달 N주차
            회의록 작성해 주세요' 와 그 링크를 채널에 새 글로 올려줘"

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
        try:
            schedule = store.resolve_schedule(
                run_at, in_minutes, repeat, until, datetime.now(KST)
            )
        except ScheduleProblem as problem:
            return str(problem)
        task = await asyncio.to_thread(
            store.create, channel, thread_ts, user_id, prompt, schedule
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
        이 채널에 걸린, 아직 끝나지 않은 예약을 보여줍니다.

        Returns:
            예약 번호·다음 실행 시각·반복·예약한 사람·할 일 목록
        """
        tasks = await asyncio.to_thread(store.list_pending, channel)
        if not tasks:
            return "이 채널에 대기 중인 예약이 없습니다."
        return "\n".join(f"{_describe(t)} <@{t.created_by}>: {t.prompt}" for t in tasks)

    @tool
    async def cancel_scheduled_task(task_id: int) -> str:
        """
        이 채널에 걸린 예약을 취소합니다. 반복 예약은 남은 회차가 모두 취소됩니다.
        번호를 모르면 list_scheduled_tasks 로 먼저 찾습니다.

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
            " 이 채널에 대기 중인 예약이 아닙니다(없거나, 이미 끝났거나, 다른 채널)."
        )

    return [schedule_task, list_scheduled_tasks, cancel_scheduled_task]


# 카드 section text 는 3000자가 한도다. 할 일과 에러는 앞부분만 싣는다.
CARD_PROMPT_CHARS = 2000
CARD_ERROR_CHARS = 500


def _card(claim: Claim, status: str, error: Exception | None = None) -> dict:
    """회차 실행 카드. 시작할 때 올리고, 끝나면 같은 카드를 결과로 고쳐 씁니다.

    Args:
        claim: 실행하는 회차
        status: 맨 앞에 보일 상태. 예: ":hourglass_flowing_sand: 실행 중"
        error: 실패했으면 그 예외

    Returns:
        dict: chat_postMessage·chat_update 에 넘길 text 와 blocks
    """
    task = claim.task
    if claim.next_run_at:
        when = f"반복 `{task.cron}` · 다음 회차 {format_time(claim.next_run_at)}"
    else:
        when = f"반복 `{task.cron}` · 마지막 회차" if task.cron else "한 번"
    prompt = escape(task.prompt[:CARD_PROMPT_CHARS])
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{status} *예약 #{task.id}*\n> {prompt}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<@{task.created_by}> 님 예약 · {when}"}
            ],
        },
    ]
    if error is not None:
        detail = escape(str(error)[:CARD_ERROR_CHARS]).replace("`", "'")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{detail}```"}}
        )
    return {"text": f"{status} 예약 #{task.id}", "blocks": blocks}


async def _run(claim: Claim, agent: ThreadAgent, client: AsyncWebClient) -> None:
    """꺼낸 회차 하나를 실행하고 결과를 표와 카드에 남깁니다.

    백그라운드 작업이라 시작하자마자 스레드에 카드를 올리고, 끝나면 그 카드를
    완료·실패로 고쳐 씁니다. 카드만 보면 지금 돌고 있는지, 어떻게 끝났는지 압니다.

    예외를 여기서 잡지 않으면 아무도 모르게 사라지고 한 번짜리 예약은 running 에
    남습니다. 잡아서 표에 기록하고 카드에 보여줍니다.
    """
    task = claim.task
    card_ts = None
    try:
        card = await client.chat_postMessage(
            channel=task.channel_id,
            thread_ts=task.thread_ts,
            **_card(claim, ":hourglass_flowing_sand: 실행 중"),
        )
        card_ts = card["ts"]
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
        await asyncio.to_thread(store.finish, task.id, repr(error))
        # 카드조차 못 올렸으면(채널 보관 등) 고칠 카드가 없다. 기록은 표와 로그다.
        if card_ts is not None:
            await client.chat_update(
                channel=task.channel_id,
                ts=card_ts,
                **_card(claim, ":x: 실패", error),
            )
        return
    await asyncio.to_thread(store.finish, task.id, None)
    await client.chat_update(
        channel=task.channel_id, ts=card_ts, **_card(claim, ":white_check_mark: 완료")
    )


async def run_due_tasks(agent: ThreadAgent, client: AsyncWebClient) -> None:
    """시각이 된 회차를 꺼내 각각 백그라운드로 실행합니다.

    스케줄러가 1분마다 부릅니다. 에이전트는 몇 분씩 걸리므로 기다리지 않고 바로
    돌아옵니다 -- 기다리면 다음 확인이 밀려 뒤 예약이 늦어집니다.

    Args:
        agent: 스레드에서 에이전트를 돌리는 콜백
        client: 슬랙 클라이언트
    """
    for claim in await asyncio.to_thread(store.claim_due, datetime.now(KST)):
        print(f"[scheduled_task] #{claim.task.id} 실행 시작")
        job = asyncio.create_task(_run(claim, agent, client))
        _running.add(job)
        job.add_done_callback(_running.discard)
