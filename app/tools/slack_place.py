"""
도구 결과를 올릴 슬랙 스레드입니다.

Athena 표·차트·첨부 파일처럼 결과를 슬랙에 올리는 도구는 이것을 받습니다.
슬랙 봇은 멘션이 온 스레드를 넘기고, MCP 처럼 스레드가 없는 곳은 None 을
넘깁니다. None 이면 결과는 도구 반환값으로만 돌아갑니다.

채널·스레드·클라이언트·say 를 따로 받으면 도구마다 "슬랙이 있나" 를 다른
조합으로 판단하게 됩니다. 넷은 늘 함께 있거나 함께 없으므로 하나로 묶습니다.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient


@dataclass(frozen=True)
class SlackPlace:
    """결과를 올릴 슬랙 스레드

    Attributes:
        client: 슬랙 클라이언트. 파일 업로드에 씁니다
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프
        say: Bolt 의 say 와 같은 모양의 글 올리기 함수
    """

    client: AsyncWebClient
    channel: str
    thread_ts: str
    say: Callable[..., Awaitable[Any]]
