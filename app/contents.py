"""
콘텐츠 비서 봇 전용 로직
"""

from .common import (
    answer,
    search_tool,
    get_web_page_from_url,
    get_create_notion_task_tool,
    get_update_notion_task_deadline_tool,
    get_update_notion_task_status_tool,
    get_create_notion_follow_up_task_tool,
    get_notion_page_tool,
    DATABASE_ID,
)


def register_contents_handlers(app_contents):
    """
    콘텐츠 봇의 이벤트 핸들러를 등록합니다.
    """

    @app_contents.event("app_mention")
    async def app_mention_contents(body, say):
        """
        슬랙에서 콘텐츠 비서를 멘션하여 대화를 시작하면 호출되는 이벤트
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
                user, slack_thread_url, DATABASE_ID, app_contents.client
            ),
            get_update_notion_task_deadline_tool(),
            get_update_notion_task_status_tool(DATABASE_ID),
            get_create_notion_follow_up_task_tool(DATABASE_ID),
            get_notion_page_tool(),
        ]
        tools = [search_tool, get_web_page_from_url] + notion_tools
        await answer(thread_ts, channel, user, text, say, app_contents.client, tools)
