"""
범용 봇 이벤트 핸들러들
"""

import asyncio
from datetime import datetime, timedelta
import logging

from slack_bolt.async_app import AsyncBoltContext, AsyncSetStatus
from slack_sdk.web.async_client import AsyncWebClient

import route_bug
import route_dev_env_infra_bug
import summarize_deployment
from .common import (
    KST,
    slack_users_list,
    answer,
    search_tool,
    get_web_page_from_url,
    get_create_notion_task_tool,
    get_update_notion_task_deadline_tool,
    get_update_notion_task_status_tool,
    get_create_notion_follow_up_task_tool,
    get_notion_page_tool,
)
from .router import route_question
from .data_analysis import answer_data_analysis

# 상수들
# Notion API 2025-09-03 버전부터 data_source_id를 직접 사용
DATA_SOURCE_ID: str = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"  # 작업 DB
PROJECT_DATA_SOURCE_ID: str = "1023943f-84d1-4223-a5a6-0c26e22d09f0"  # 프로젝트 DB

SLACK_DAILY_SCRUM_CHANNEL_ID = "C02JX95U7AP"
SLACK_DAILY_SCRUM_CANVAS_ID = "F05S8Q78CGZ"
SLACK_BUG_REPORT_CHANNEL_ID = "C07A5HVG6UR"
SLACK_DEV_ENV_INFRA_BUG_CHANNEL_ID = "C096HGFDFM1"

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
        event = body.get("event")

        if event is None:
            return

        thread_ts = event.get("thread_ts") or body["event"]["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]
        # Slack 스레드 링크 만들기
        slack_workspace = "monolith-keb2010"
        thread_ts_for_link = (event.get("thread_ts") or body["event"]["ts"]).replace(
            ".", ""
        )
        slack_thread_url = (
            f"https://{slack_workspace}.slack.com"
            f"/archives/{channel}/p{thread_ts_for_link}"
        )

        # 질문을 라우팅
        agent_type = await route_question(text)

        if agent_type == "data_analysis":
            # 데이터 분석 Agent 사용
            # 스레드의 모든 메시지를 가져옴
            result = await app.client.conversations_replies(
                channel=channel, ts=thread_ts
            )

            # 메시지에서 사용자 ID를 수집
            user_ids = set(
                message["user"] for message in result["messages"] if "user" in message
            )
            if user:
                user_ids.add(user)

            # 사용자 정보 일괄 조회
            user_info_list = await slack_users_list(app.client)
            user_dict = {
                user["id"]: user
                for user in user_info_list["members"]
                if user["id"] in user_ids
            }

            threads = []
            for message in result["messages"][:-1]:
                slack_user_id = message.get("user", None)
                if slack_user_id:
                    user_profile = user_dict.get(slack_user_id, {})
                    user_real_name = user_profile.get("real_name", "Unknown")
                else:
                    user_real_name = "Bot"
                threads.append(f"{user_real_name}:\n{message['text']}")

            # 최종 질의한 사용자 정보
            slack_user_id = user
            user_profile = user_dict.get(slack_user_id, {})
            user_real_name = user_profile.get("real_name", "Unknown")

            threads_joined = "\n\n".join(threads)

            await answer_data_analysis(
                thread_ts,
                channel,
                user_real_name,
                threads_joined,
                text,
                say,
                app.client,
            )
        else:
            # 기존 General Agent 사용
            notion_tools = [
                get_create_notion_task_tool(
                    user,
                    slack_thread_url,
                    DATA_SOURCE_ID,
                    app.client,
                    PROJECT_DATA_SOURCE_ID,
                ),
                get_update_notion_task_deadline_tool(),
                get_update_notion_task_status_tool(DATA_SOURCE_ID),
                get_create_notion_follow_up_task_tool(DATA_SOURCE_ID),
                get_notion_page_tool(),
            ]
            tools = [search_tool, get_web_page_from_url] + notion_tools
            await answer(thread_ts, channel, user, text, say, app.client, tools)

    @app.event("message")
    async def message(body, say):
        """
        버그 신고 채널에 올라오는 메시지를 LLM으로 분석하여
        Notion에 버그 작업을 생성하고, 시급한 경우 담당 그룹을 태그합니다.
        """
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

        notion_tools = [
            get_create_notion_task_tool(
                context.user_id,
                slack_thread_url,
                DATA_SOURCE_ID,
                app.client,
                PROJECT_DATA_SOURCE_ID,
            ),
            get_update_notion_task_deadline_tool(),
            get_update_notion_task_status_tool(DATA_SOURCE_ID),
            get_create_notion_follow_up_task_tool(DATA_SOURCE_ID),
            get_notion_page_tool(),
        ]
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

    @app.command("/summarize-deployment")
    async def on_summarize_deployment(ack, body):
        """
        /summarize-deployment 명령어를 처리하는 핸들러
        """
        await ack(text="⏳ 배포 요약을 작성 중입니다…")

        # summarize_deployment 가 Blocking IO 이므로
        # asyncio.to_thread 를 사용하여 비동기적으로 실행
        # 그렇지 않으면 ack 응답이 3초안에 날아가지 않아
        # dispatch_failed 오류가 발생함.
        # https://chatgpt.com/share/6805f405-36b0-8002-a298-ac2cefb12b0b
        await asyncio.to_thread(
            summarize_deployment.summarize_deployment,
            caller_slack_user_id=body.get("user_id"),
        )
