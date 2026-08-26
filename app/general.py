"""
범용 봇 이벤트 핸들러들
"""

import asyncio
from datetime import datetime, timedelta
import importlib
import traceback

from cachetools import TTLCache
from slack_bolt.context.respond.async_respond import AsyncRespond
from slack_sdk.web.async_client import AsyncWebClient

from . import analyze_oom, route_bug, route_dev_env_infra_bug
from .knowledge import get_knowledge_channel_tools, get_knowledge_query_tools
from .sms import get_sms_tools
from .task_list import get_channel_task_list_tools, get_task_list_write_tools
from .event_dedup import is_duplicate_event
from .common import (
    KST,
    answer,
    search_tool,
    get_web_page_from_url,
    get_create_notion_task_tool,
    get_update_notion_task_deadline_tool,
    get_update_notion_task_status_tool,
    get_create_notion_follow_up_task_tool,
    get_notion_page_tool,
)
from service.config import load_config, Squad
from service.slack_task_list import find_channel_task_list

# 상수들
# Notion API 2025-09-03 버전부터 data_source_id를 직접 사용
DATA_SOURCE_ID: str = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"  # 작업 DB (기본값)
PROJECT_DATA_SOURCE_ID: str = "1023943f-84d1-4223-a5a6-0c26e22d09f0"  # 프로젝트 DB

# 유저그룹 멤버 캐시 (1시간 TTL)
_cache_usergroup_members: TTLCache = TTLCache(maxsize=20, ttl=3600)

# create_task 참조를 유지하여 GC 방지
_background_jobs: set[asyncio.Task] = set()


async def _get_user_squad(client: AsyncWebClient, user_id: str | None) -> Squad | None:
    """사용자가 속한 스쿼드를 결정합니다.

    각 스쿼드의 Slack usergroup 멤버 목록을 조회하여
    사용자가 포함된 첫 번째 스쿼드를 반환합니다.
    """
    if user_id is None:
        return None

    config = load_config()

    # 사용자별 오버라이드 확인 (복수 스쿼드 소속 시 우선 스쿼드 지정)
    if user_id in config.squad_overrides:
        return config.squad_overrides[user_id]

    for squad in config.squads:
        if squad.slack_usergroup_id is None:
            continue
        cache_key = f"usergroup_{squad.slack_usergroup_id}"
        if cache_key not in _cache_usergroup_members:
            try:
                resp = await client.usergroups_users_list(
                    usergroup=squad.slack_usergroup_id
                )
                _cache_usergroup_members[cache_key] = resp["users"]
            except Exception:
                # usergroup 조회 실패 시 빈 리스트로 처리
                _cache_usergroup_members[cache_key] = []
        if user_id in _cache_usergroup_members[cache_key]:
            return squad
    return None


async def _build_tools(
    client: AsyncWebClient, user_id: str | None, channel: str, thread_ts: str
) -> list:
    """질문자의 스쿼드에 맞춘 도구 목록을 만듭니다.

    작업 생성 도구는 스쿼드별 Notion DB를 대상으로 하며,
    후속 작업 도구는 프로젝트/구성요소 속성이 있는 메인 DB에서만 사용합니다.

    작업 리스트로 등록된 채널은 예외입니다. 작업이 노션이 아니라 그 채널의
    슬랙 리스트로 가고, 노션 작업 생성 도구 대신 리스트 도구가 들어갑니다.

    Args:
        client: 슬랙 클라이언트
        user_id: 질문자의 Slack 사용자 ID
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프

    Returns:
        list: 에이전트에 주입할 도구 목록
    """
    slack_workspace = "monolith-keb2010"
    slack_thread_url = (
        f"https://{slack_workspace}.slack.com"
        f"/archives/{channel}/p{thread_ts.replace('.', '')}"
    )

    squad = await _get_user_squad(client, user_id)
    if squad and squad.notion_db and squad.notion_db.name != "main":
        task_ds_id = squad.notion_db.data_source_id
        title_prop = squad.notion_db.properties.title
        project_ds_id = None
    else:
        task_ds_id = DATA_SOURCE_ID
        title_prop = "제목"
        project_ds_id = PROJECT_DATA_SOURCE_ID

    # 작업 리스트로 등록된 채널은 작업을 노션이 아니라 그 리스트에 만든다.
    # 노션 작업 생성 도구를 같이 주면 에이전트가 둘 사이에서 흔들린다.
    # DM 은 리스트를 붙일 수 없으므로 조회하지 않는다.
    task_list = None
    if not channel.startswith("D"):
        task_list = await asyncio.to_thread(find_channel_task_list, channel)
    if task_list:
        create_tools = get_task_list_write_tools(
            client, task_list, user_id, slack_thread_url
        )
    else:
        create_tools = [
            get_create_notion_task_tool(
                user_id,
                slack_thread_url,
                task_ds_id,
                client,
                project_ds_id,
                title_prop,
            )
        ]
        if project_ds_id:
            create_tools.append(get_create_notion_follow_up_task_tool(task_ds_id))

    notion_tools = [
        get_update_notion_task_deadline_tool(),
        get_update_notion_task_status_tool(task_ds_id),
        get_notion_page_tool(),
    ]

    return (
        [search_tool, get_web_page_from_url]
        + create_tools
        + notion_tools
        + get_knowledge_channel_tools(client, channel)
        + get_knowledge_query_tools(client, user_id)
        + get_channel_task_list_tools(client, channel)
        + get_sms_tools(client, channel, thread_ts)
    )


def get_sms_revise(app):
    """문자 초안 수정 피드백을 초안을 쓴 에이전트에게 되돌리는 콜백을 만듭니다.

    카드의 [수정] 은 모달로 피드백만 받습니다. 그 피드백을 스레드의 다음
    질문처럼 다시 태워야 에이전트가 앞의 대화(수신자·문안·의도)를 그대로 두고
    문안만 고쳐 새 초안을 올립니다.

    app/sms.py 가 아니라 여기서 만듭니다. 저쪽에서 이 모듈을 부르면
    general → sms → general 로 순환합니다.

    Args:
        app: slack_bolt AsyncApp

    Returns:
        `async (channel, thread_ts, user, text) -> None` 콜백
    """

    async def revise(channel: str, thread_ts: str, user: str, text: str) -> None:
        async def say(payload, thread_ts=thread_ts):
            await app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=payload.get("text", "문자 초안을 다시 썼습니다"),
                **{k: v for k, v in payload.items() if k != "text"},
            )

        tools = await _build_tools(app.client, user, channel, thread_ts)
        await answer(thread_ts, channel, user, text, say, app.client, tools)

    return revise


SLACK_DAILY_SCRUM_CHANNEL_ID = "C02JX95U7AP"
SLACK_DAILY_SCRUM_CANVAS_ID = "F05S8Q78CGZ"
SLACK_BUG_REPORT_CHANNEL_ID = "C07A5HVG6UR"
SLACK_DEV_ENV_INFRA_BUG_CHANNEL_ID = "C096HGFDFM1"

# 버그 신고 채널은 제품별로 나뉘어 있으므로, 채널이 곧 제품이다.
# 값은 service.teams.PRODUCT_USERGROUP_IDS 의 키와 같아야 한다.
BUG_REPORT_CHANNEL_TO_PRODUCT = {
    SLACK_BUG_REPORT_CHANNEL_ID: "코들",
}

USER_ID_TO_LAST_HUDDLE_JOINED_AT = {}


async def _run_wa_job(
    func,
    args: list[str],
    kwargs: dict,
    description: str,
    respond: AsyncRespond,
) -> None:
    """
    `/wa` 작업을 스레드에서 실행하고 결과를 실행한 사람에게만 보여줍니다.

    ack 이후에는 응답 경로가 response_url 뿐이라, 예외를 여기서 잡지 않으면 실패가 파드
    로그에만 남고 사용자에게는 "진행 중" 에서 멈춘 것처럼 보인다.

    Args:
        func: 작업 함수
        args: 작업 함수의 위치 인자
        kwargs: 작업 함수의 키워드 인자
        description: 작업 표의 설명. 실패 안내에 쓴다
        respond: 슬래시 커맨드의 response_url 응답 함수
    """
    try:
        result = await asyncio.to_thread(func, *args, **kwargs)
    except Exception as error:
        traceback.print_exc()
        await respond(f":x: {description} 에 실패했습니다.\n```{error}```")
        return
    if isinstance(result, str):
        await respond(result)


def register_general_handlers(app):
    """
    범용 봇의 이벤트 핸들러를 등록합니다.
    """

    @app.event("app_mention")
    async def app_mention(body, say):
        """
        슬랙에서 로봇을 멘션하여 대화를 시작하면 호출되는 이벤트
        """
        if is_duplicate_event(body):
            return

        event = body.get("event")

        if event is None:
            return

        # 봇이 보낸 메시지는 무시 (자기 자신을 태그하는 무한 루프 방지)
        if event.get("bot_id"):
            return

        thread_ts = event.get("thread_ts") or body["event"]["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        # OOM 분석 요청 감지 (특정 채널의 스레드에서 "분석" 키워드 멘션)
        # 예: "@봇 분석해줘", "@봇 이 알림 분석해주세요"
        if channel == "C07B6FT3R5L" and "분석" in text and event.get("thread_ts"):
            await analyze_oom.analyze_oom_alert(app.client, body, say)
            return

        tools = await _build_tools(app.client, user, channel, thread_ts)
        await answer(thread_ts, channel, user, text, say, app.client, tools)

    @app.event("message")
    async def message(body, say):
        """
        DM으로 받은 질문에 답하고,
        버그 신고 채널에 올라오는 메시지를 LLM으로 분석하여
        Notion에 버그 작업을 생성하고, 시급한 경우 담당 그룹을 태그합니다.
        """
        if is_duplicate_event(body):
            return

        event = body.get("event", {})
        channel = event.get("channel")

        if event.get("channel_type") == "im":
            # 봇 메시지와 편집·삭제 등 서브타입 이벤트는 무시
            if event.get("bot_id") or event.get("subtype"):
                return

            thread_ts = event.get("thread_ts") or event["ts"]
            user = event.get("user")
            tools = await _build_tools(app.client, user, channel, thread_ts)
            await answer(
                thread_ts, channel, user, event["text"], say, app.client, tools
            )
        elif channel in BUG_REPORT_CHANNEL_TO_PRODUCT:
            # 메시지 편집 이벤트 필터링
            subtype = event.get("subtype")
            print(f"Subtype: {subtype}")
            if subtype != "bot_message":
                print("Skipping non-bot message")
                return

            thread_ts = event.get("thread_ts")
            message_ts = event.get("ts")
            print(f"Thread TS: {thread_ts}, Message TS: {message_ts}")

            if thread_ts is None or thread_ts == message_ts:
                print("Routing bug report")
                await route_bug.route_bug(
                    app.client, body, BUG_REPORT_CHANNEL_TO_PRODUCT[channel]
                )
        elif channel == SLACK_DEV_ENV_INFRA_BUG_CHANNEL_ID:
            # 메시지 편집 이벤트 필터링
            subtype = event.get("subtype")
            print(f"Subtype: {subtype}")
            if subtype != "bot_message":
                print("Skipping non-bot message")
                return

            thread_ts = event.get("thread_ts")
            message_ts = event.get("ts")
            print(f"Thread TS: {thread_ts}, Message TS: {message_ts}")

            if thread_ts is None or thread_ts == message_ts:
                print("Routing dev env infra bug report")
                await route_dev_env_infra_bug.route_dev_env_infra_bug(app.client, body)

    # 자동화 작업 표 — 단일 진입 커맨드 `/wa <작업>` 로 라우팅한다.
    # 슬랙 앱 UI 에는 `/wa` 하나만 등록하면 되고, 새 작업은 이 표에 한 줄만 추가한다.
    # 각 튜플: (작업명, module_path, func_name, description[, body_kwargs])
    # 작업명 뒤에 남은 토큰은 함수의 위치 인자로 전달한다.
    # 예: `/wa reset-develop jce-class-rails` → func("jce-class-rails")
    # body_kwargs 는 선택사항으로, body 에서 값을 꺼내 함수 키워드 인자로 전달할 매핑이다.
    # 예: {"caller_slack_user_id": "user_id"} → func(caller_slack_user_id=body.get("user_id"))
    # 함수가 문자열을 반환하면 실행한 사람에게만 보이는 응답으로 되돌려준다.
    _JOBS = [
        (
            "validate-customer-reports",
            "scripts.validate_customer_reports",
            "main",
            "고객 보고서 검증",
        ),
        (
            "manage-tasks-daily",
            "scripts.manage_tasks_daily",
            "main",
            "일일 작업 알림 처리",
        ),
        (
            "notify-upcoming-workevent",
            "scripts.notify_upcoming_workevent",
            "main",
            "근태 예정 알림 생성",
        ),
        (
            "notify-worktime-left",
            "scripts.notify_worktime_left",
            "main",
            "잔여 근무시간 계산",
        ),
        (
            "collect-review-stats",
            "scripts.collect_review_stats",
            "main",
            "리뷰 통계 수집",
        ),
        (
            "collect-coding-rule-feedbacks",
            "scripts.collect_coding_rule_feedbacks",
            "main",
            "코딩 규칙 피드백 수집",
        ),
        (
            "post-scrum-message",
            "scripts.post_scrum_message",
            "main",
            "스크럼 메시지 발송",
        ),
        (
            "schedule-scrum-mention",
            "scripts.schedule_scrum_mention",
            "main",
            "스크럼 멘션 발송",
        ),
        (
            "summarize-deployment",
            "app.summarize_deployment",
            "summarize_deployment",
            "배포 요약을 작성",
            {"caller_slack_user_id": "user_id"},
        ),
        (
            "crawl-education-bids",
            "scripts.crawl_education_bids",
            "main",
            "교육 외주 입찰공고 수집·평가",
        ),
        (
            "reset-develop",
            "scripts.reset_develop",
            "main",
            "develop 을 main 으로 초기화",
            {"caller_slack_user_id": "user_id"},
        ),
    ]

    _JOB_BY_SUB = {
        sub: (mod, fn, desc, rest[0] if rest else None)
        for sub, mod, fn, desc, *rest in _JOBS
    }

    def _wa_usage():
        lines = ["사용법: `/wa <작업> [인자]`", "", "작업 목록:"]
        lines += [f"• `{sub}` — {meta[2]}" for sub, meta in _JOB_BY_SUB.items()]
        return "\n".join(lines)

    @app.command("/wa")
    async def handle_wa(ack, body, respond):
        text = (body.get("text") or "").strip()
        sub = text.split()[0] if text else ""
        if sub in ("", "help", "list"):
            await ack(text=_wa_usage())
            return
        meta = _JOB_BY_SUB.get(sub)
        if not meta:
            await ack(text=f"알 수 없는 작업: `{sub}`\n\n{_wa_usage()}")
            return
        module_path, func_name, description, body_kwargs = meta
        await ack(text=f"⏳ {description} 중입니다…")
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        args = text.split()[1:]
        kwargs = {kw: body.get(bk) for kw, bk in (body_kwargs or {}).items()}
        # 슬래시 커맨드 응답은 리스너가 끝나야 나가므로, 작업을 기다리면 3초 제한에 걸려
        # ack 대신 타임아웃이 뜬다. 작업 결과와 실패는 _run_wa_job 이 response_url 로 보낸다.
        task = asyncio.create_task(
            _run_wa_job(func, args, kwargs, description, respond)
        )
        _background_jobs.add(task)
        task.add_done_callback(_background_jobs.discard)
