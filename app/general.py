"""
범용 봇 이벤트 핸들러들
"""

import asyncio
from datetime import datetime, timedelta
import importlib
import logging

from cachetools import TTLCache
from slack_bolt.async_app import AsyncBoltContext, AsyncSetStatus
from slack_sdk.web.async_client import AsyncWebClient

from . import analyze_oom, create_deal, route_bug, route_dev_env_infra_bug
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

# 상수들
# Notion API 2025-09-03 버전부터 data_source_id를 직접 사용
DATA_SOURCE_ID: str = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"  # 작업 DB (기본값)
PROJECT_DATA_SOURCE_ID: str = "1023943f-84d1-4223-a5a6-0c26e22d09f0"  # 프로젝트 DB

# 유저그룹 멤버 캐시 (1시간 TTL)
_cache_usergroup_members: TTLCache = TTLCache(maxsize=20, ttl=3600)


async def _get_user_squad(client: AsyncWebClient, user_id: str | None) -> Squad | None:
    """사용자가 속한 스쿼드를 결정합니다.

    각 스쿼드의 Slack usergroup 멤버 목록을 조회하여
    사용자가 포함된 첫 번째 스쿼드를 반환합니다.
    """
    if user_id is None:
        return None

    config = load_config()
    for squad in config.squads:
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


SLACK_DAILY_SCRUM_CHANNEL_ID = "C02JX95U7AP"
SLACK_DAILY_SCRUM_CANVAS_ID = "F05S8Q78CGZ"
SLACK_BUG_REPORT_CHANNEL_ID = "C07A5HVG6UR"
SLACK_DEV_ENV_INFRA_BUG_CHANNEL_ID = "C096HGFDFM1"
SLACK_DEAL_FORM_CHANNEL_ID = "C0B0Z76KFGF"

USER_ID_TO_LAST_HUDDLE_JOINED_AT = {}


def register_general_handlers(app, assistant):
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

        # 딜 채널에서 멘션 시: 스레드 내용을 분석하여 Notion 딜 페이지 생성
        if channel == SLACK_DEAL_FORM_CHANNEL_ID:
            await create_deal.create_deal(app.client, body)
            return

        # Slack 스레드 링크 만들기
        slack_workspace = "monolith-keb2010"
        thread_ts_for_link = (event.get("thread_ts") or body["event"]["ts"]).replace(
            ".", ""
        )
        slack_thread_url = (
            f"https://{slack_workspace}.slack.com"
            f"/archives/{channel}/p{thread_ts_for_link}"
        )

        # 사용자의 스쿼드에 따라 대상 Notion DB 결정
        squad = await _get_user_squad(app.client, user)
        if squad and squad.notion_db.name != "main":
            task_ds_id = squad.notion_db.data_source_id
            title_prop = squad.notion_db.properties.title
            project_ds_id = None
        else:
            task_ds_id = DATA_SOURCE_ID
            title_prop = "제목"
            project_ds_id = PROJECT_DATA_SOURCE_ID

        # General Agent 사용
        notion_tools = [
            get_create_notion_task_tool(
                user,
                slack_thread_url,
                task_ds_id,
                app.client,
                project_ds_id,
                title_prop,
            ),
            get_update_notion_task_deadline_tool(),
            get_update_notion_task_status_tool(task_ds_id),
            get_notion_page_tool(),
        ]
        # 후속 작업 도구는 메인 DB에서만 사용 (프로젝트/구성요소 속성 필요)
        if project_ds_id:
            notion_tools.append(get_create_notion_follow_up_task_tool(task_ds_id))

        tools = [search_tool, get_web_page_from_url] + notion_tools
        await answer(thread_ts, channel, user, text, say, app.client, tools)

    @app.event("message")
    async def message(body, say):
        """
        버그 신고 채널에 올라오는 메시지를 LLM으로 분석하여
        Notion에 버그 작업을 생성하고, 시급한 경우 담당 그룹을 태그합니다.
        """
        if is_duplicate_event(body):
            return

        print("Received message event:", body)

        event = body.get("event", {})
        channel = event.get("channel")
        print(f"Channel: {channel}")

        if channel == SLACK_BUG_REPORT_CHANNEL_ID:
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
                await route_bug.route_bug(app.client, body)
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
    @assistant.thread_started
    async def start_assistant_thread(say, _set_suggested_prompts):
        """
        Assistant thread started
        """
        await say(":wave: 안녕하세요. 무엇을 도와드릴까요?")

    @assistant.user_message
    async def respond_in_assistant_thread(
        payload: dict,
        logger: logging.Logger,
        context: AsyncBoltContext,
        set_status: AsyncSetStatus,
        client: AsyncWebClient,
        say,
    ):
        """
        Respond to a user message in the assistant thread.
        """
        # Slack 스레드 링크 만들기
        slack_workspace = "monolith-keb2010"
        thread_ts_for_link = context.thread_ts.replace(".", "")
        slack_thread_url = (
            f"https://{slack_workspace}.slack.com"
            f"/archives/{context.channel_id}/p{thread_ts_for_link}"
        )

        # 사용자의 스쿼드에 따라 대상 Notion DB 결정
        squad = await _get_user_squad(app.client, context.user_id)
        if squad and squad.notion_db.name != "main":
            task_ds_id = squad.notion_db.data_source_id
            title_prop = squad.notion_db.properties.title
            project_ds_id = None
        else:
            task_ds_id = DATA_SOURCE_ID
            title_prop = "제목"
            project_ds_id = PROJECT_DATA_SOURCE_ID

        notion_tools = [
            get_create_notion_task_tool(
                context.user_id,
                slack_thread_url,
                task_ds_id,
                app.client,
                project_ds_id,
                title_prop,
            ),
            get_update_notion_task_deadline_tool(),
            get_update_notion_task_status_tool(task_ds_id),
            get_notion_page_tool(),
        ]
        if project_ds_id:
            notion_tools.append(get_create_notion_follow_up_task_tool(task_ds_id))

        tools = [search_tool, get_web_page_from_url] + notion_tools

        await answer(
            context.thread_ts,
            context.channel_id,
            context.user_id,
            payload["text"],
            say,
            app.client,
            tools,
        )

    # 크론 작업들에 대한 슬래시 커맨드 일괄 등록
    # 각 튜플: (command, module_path, func_name, description[, body_kwargs])
    # body_kwargs 는 선택사항으로, body 에서 값을 꺼내 함수 키워드 인자로
    # 전달할 매핑입니다.  예: {"caller_slack_user_id": "user_id"} 이면
    # func(caller_slack_user_id=body.get("user_id")) 로 호출됩니다.
    _CRON_COMMANDS = [
        (
            "/validate-customer-reports",
            "scripts.validate_customer_reports",
            "main",
            "고객 보고서 검증",
        ),
        (
            "/manage-tasks-daily",
            "scripts.manage_tasks_daily",
            "main",
            "일일 작업 알림 처리",
        ),
        (
            "/notify-upcoming-workevent",
            "scripts.notify_upcoming_workevent",
            "main",
            "근태 예정 알림 생성",
        ),
        (
            "/notify-worktime-left",
            "scripts.notify_worktime_left",
            "main",
            "잔여 근무시간 계산",
        ),
        (
            "/collect-review-stats",
            "scripts.collect_review_stats",
            "main",
            "리뷰 통계 수집",
        ),
        (
            "/collect-coding-rule-feedbacks",
            "scripts.collect_coding_rule_feedbacks",
            "main",
            "코딩 규칙 피드백 수집",
        ),
        (
            "/post-scrum-message",
            "scripts.post_scrum_message",
            "main",
            "스크럼 메시지 발송",
        ),
        (
            "/schedule-scrum-mention",
            "scripts.schedule_scrum_mention",
            "main",
            "스크럼 멘션 발송",
        ),
        (
            "/summarize-deployment",
            "app.summarize_deployment",
            "summarize_deployment",
            "배포 요약을 작성",
            {"caller_slack_user_id": "user_id"},
        ),
        (
            "/announce-deployment-rotation",
            "scripts.announce_deployment_rotation",
            "main",
            "배포 담당자 공지",
        ),
    ]

    def _register_cron_command(
        command_name, module_path, func_name, description, body_kwargs=None
    ):
        @app.command(command_name)
        async def handler(ack, body):
            await ack(text=f"⏳ {description} 중입니다…")
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            kwargs = {
                kwarg: body.get(body_key)
                for kwarg, body_key in (body_kwargs or {}).items()
            }
            await asyncio.to_thread(func, **kwargs)

    for cmd, mod, fn, desc, *rest in _CRON_COMMANDS:
        _register_cron_command(cmd, mod, fn, desc, rest[0] if rest else None)
