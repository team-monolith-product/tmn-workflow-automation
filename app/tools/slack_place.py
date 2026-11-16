"""
도구 결과를 올릴 슬랙 스레드입니다.

Athena 표·차트·첨부 파일처럼 결과를 슬랙에 올리는 도구는 이것을 받습니다.
슬랙 봇은 멘션이 온 스레드를 넘기고, MCP 처럼 스레드가 없는 곳은 None 을
넘깁니다. None 이면 결과는 도구 반환값으로만 돌아갑니다.

채널·스레드·클라이언트를 따로 받으면 도구마다 "슬랙이 있나" 를 다른 조합으로
판단하게 됩니다. 셋은 늘 함께 있거나 함께 없으므로 하나로 묶습니다.
"""

from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse


@dataclass(frozen=True)
class SlackPlace:
    """결과를 올릴 슬랙 스레드

    Attributes:
        client: 슬랙 클라이언트
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프
    """

    client: AsyncWebClient
    channel: str
    thread_ts: str

    async def post(
        self, payload: dict[str, Any], thread_ts: str | None = None
    ) -> AsyncSlackResponse:
        """스레드에 글을 올립니다.

        Bolt 의 say 와 같은 모양이라 say 를 받는 곳(answer, ToolStatusHandler)에
        그대로 넘길 수 있습니다. 슬랙 이벤트가 없어도 씁니다.

        Args:
            payload: chat.postMessage 인자. blocks 만 있어도 됩니다
            thread_ts: 다른 스레드에 올릴 때만 줍니다

        Returns:
            AsyncSlackResponse: 올린 글. ToolStatusHandler 가 ts 를 꺼내 고칩니다
        """
        return await self.client.chat_postMessage(
            channel=self.channel,
            thread_ts=thread_ts or self.thread_ts,
            # 표·코드 블록처럼 blocks 만 오는 글도 있다. 알림 미리보기에 쓸 글자다.
            text=payload.get("text", "로봇 답변"),
            **{k: v for k, v in payload.items() if k != "text"},
        )
