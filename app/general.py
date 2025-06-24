"""
범용 봇 이벤트 핸들러들
"""

import asyncio
from datetime import datetime, timedelta
import logging

from slack_bolt.async_app import AsyncBoltContext, AsyncSetStatus
from slack_sdk.web.async_client import AsyncWebClient

import route_bug
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

# 상수들
DATABASE_ID: str = "a9de18b3877c453a8e163c2ee1ff4137"

SLACK_DAILY_SCRUM_CHANNEL_ID = "C02JX95U7AP"
SLACK_DAILY_SCRUM_CANVAS_ID = "F05S8Q78CGZ"
SLACK_BUG_REPORT_CHANNEL_ID = "C07A5HVG6UR"

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

        notion_tools = [
            get_create_notion_task_tool(
                user,
                slack_thread_url,
                DATABASE_ID,
                app.client,
                "9df81e8ee45e4f49aceb402c084b3ac7",
            ),
            get_update_notion_task_deadline_tool(),
            get_update_notion_task_status_tool(DATABASE_ID),
            get_create_notion_follow_up_task_tool(DATABASE_ID),
            get_notion_page_tool(),
        ]
        tools = [search_tool, get_web_page_from_url] + notion_tools
        await answer(thread_ts, channel, user, text, say, app.client, tools)

    @app.event("user_huddle_changed")
    async def user_huddle_changed(body, say):
        """
        사용자가 huddle을 변경할 때 호출되는 이벤트
        """
        event_ts = body.get("event", {}).get("event_ts")
        response = await app.client.conversations_history(
            channel=SLACK_DAILY_SCRUM_CHANNEL_ID, latest=event_ts, limit=1
        )

        print(response)

        messages = response.data.get("messages")
        if not messages:
            return

        print(messages)

        message = messages[0]
        if not message:
            return

        print(message)

        room = message.get("room")
        if not room:
            return

        print(room)

        participants = room.get("participants")
        if not participants:
            return

        print(participants)

        # 사용자 정보 일괄 조회
        user_info_list = await slack_users_list(app.client)
        user_dict = {user["id"]: user for user in user_info_list["members"]}
        for participant in participants:
            # 최근 허들 참여 시간 업데이트를 했다면 절차를 생략함.
            # 30분
            last_joined_at = USER_ID_TO_LAST_HUDDLE_JOINED_AT.get(participant)
            if last_joined_at and (datetime.now(tz=KST) - last_joined_at) < timedelta(
                minutes=30
            ):
                # 30분 이내에 허들에 참여한 이력이 있다면 생략
                continue
            USER_ID_TO_LAST_HUDDLE_JOINED_AT[participant] = datetime.now(tz=KST)

            user_name = user_dict[participant]["real_name"]

            sections_resp = await app.client.canvases_sections_lookup(
                canvas_id=SLACK_DAILY_SCRUM_CANVAS_ID,
                criteria={"contains_text": user_dict[participant]["real_name"]},
            )
            sections = sections_resp["sections"]
            for section in sections:
                await app.client.canvases_edit(
                    canvas_id=SLACK_DAILY_SCRUM_CANVAS_ID,
                    changes=[
                        {
                            "operation": "replace",
                            "section_id": section["id"],
                            "document_content": {
                                "type": "markdown",
                                "markdown": f"- [x] {user_name} :heart:\n",
                            },
                        }
                    ],
                )

    @app.event("message")
    async def message(body, say):
        """
        버그 신고 채널에 올라오는 메시지를 LLM으로 분석하여
        Notion에 버그 작업을 생성하고, 시급한 경우 담당 그룹을 태그합니다.
        """
        event = body.get("event", {})
        channel = event.get("channel")
        if channel != SLACK_BUG_REPORT_CHANNEL_ID:
            return

        # 메시지 편집 이벤트 필터링
        subtype = event.get("subtype")
        if subtype != "bot_message":
            return

        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts")
        if thread_ts is None or thread_ts == message_ts:
            await route_bug.route_bug(app.client, body)

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
                DATABASE_ID,
                app.client,
                "9df81e8ee45e4f49aceb402c084b3ac7",
            ),
            get_update_notion_task_deadline_tool(),
            get_update_notion_task_status_tool(DATABASE_ID),
            get_create_notion_follow_up_task_tool(DATABASE_ID),
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
