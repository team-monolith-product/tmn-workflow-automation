"""Slack 메시지 위치와 permalink 변환을 제공합니다."""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from slack_sdk.web.async_client import AsyncWebClient


@dataclass(frozen=True)
class SlackMessageLocation:
    """Slack 메시지와 그 메시지가 속한 루트 스레드 위치입니다."""

    channel_id: str
    ts: str
    root_ts: str
    permalink: str | None = None


def message_location(reference: dict[str, Any]) -> SlackMessageLocation:
    """List message 셀의 참조를 Slack API 호출 위치로 바꿉니다."""
    permalink = reference.get("value") or reference.get("permalink")
    channel_id = reference.get("channel_id")
    ts = reference.get("ts")
    thread_ts = reference.get("thread_ts")

    if permalink:
        parsed = urlparse(str(permalink))
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 3 and re.fullmatch(r"[CDG][A-Z0-9]+", segments[-2]):
            channel_id = channel_id or segments[-2]
            path_ts = segments[-1]
            if path_ts.startswith("p") and path_ts[1:].isdigit():
                digits = path_ts[1:]
                if len(digits) > 6:
                    ts = ts or f"{digits[:-6]}.{digits[-6:]}"
        query = parse_qs(parsed.query)
        thread_ts = thread_ts or (query.get("thread_ts") or [None])[0]
        channel_id = channel_id or (query.get("cid") or [None])[0]

    if not channel_id or not ts:
        raise ValueError("List의 Slack 메시지 링크에서 채널과 시각을 읽지 못했습니다.")

    return SlackMessageLocation(
        channel_id=str(channel_id),
        ts=str(ts),
        root_ts=str(thread_ts or ts),
        permalink=str(permalink) if permalink else None,
    )


async def get_permalink(
    client: AsyncWebClient, location: SlackMessageLocation, root: bool = False
) -> str:
    """Slack 메시지 또는 그 루트 스레드의 permalink를 반환합니다."""
    if location.permalink and not root:
        return location.permalink
    message_ts = location.root_ts if root else location.ts
    response = await client.chat_getPermalink(
        channel=location.channel_id, message_ts=message_ts
    )
    return response["permalink"]
