"""
에이전트가 슬랙에 직접 글을 올리는 도구입니다.

보통 답변은 도구 없이 스레드에 답글로 남습니다. 이 도구는 공지처럼 채널 본문에
보여야 할 때만 씁니다. 예약 작업의 정기 공지가 주로 씁니다.
"""

from langchain_core.tools import StructuredTool, tool
from slack_sdk.web.async_client import AsyncWebClient


def get_post_channel_message_tool(
    client: AsyncWebClient, channel: str
) -> StructuredTool:
    """지금 채널에 새 글을 올리는 도구를 돌려줍니다.

    Args:
        client: 슬랙 클라이언트
        channel: 글을 올릴 채널 ID. 지금 대화 중인 채널만 줍니다.

    Returns:
        StructuredTool: 채널 새 글 도구
    """

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

    return post_channel_message
