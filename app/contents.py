"""
콘텐츠 비서 봇 전용 로직
"""

from .common import (
    answer,
    search_tool,
    get_web_page_from_url,
    get_notion_tools,
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

        notion_tools = await get_notion_tools(user, None, app_contents.client)
        tools = [search_tool, get_web_page_from_url] + notion_tools
        await answer(thread_ts, channel, user, text, say, app_contents.client, tools)
