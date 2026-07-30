"""
슬랙 대화 조회 LangChain Tools

conversations.history는 최상위 메시지만 돌려주고 스레드 답글은 주지 않는다.
메시지마다 conversations.replies를 부르면 요청이 폭증하므로, 2단계로 찾는다.
최상위에서 후보를 좁힌 뒤 그 스레드만 깊이 읽는다.
"""

import re
from datetime import datetime, timedelta
from typing import Annotated

from cachetools import TTLCache
from langchain_core.tools import tool
from slack_sdk.errors import SlackApiError

from ..common import KST, slack_users_list

# conversations.history 요청당 최대치. 내부 앱은 이 한도를 그대로 쓴다.
MAX_HISTORY = 999

# 깊이 읽을 스레드 수. 이 수만큼 conversations.replies 요청이 추가된다.
MAX_THREADS = 5

# 반환할 최대 메시지 수 (에이전트 컨텍스트 보호)
MAX_MESSAGES = 40

_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)>")

# 봇이 들어가 있는 채널 목록. 자주 바뀌지 않으므로 캐시한다.
_channels_cache = TTLCache(maxsize=1, ttl=600)


async def _bot_channels(slack_client) -> dict[str, str]:
    """
    봇이 참여한 채널의 {채널ID: 채널명} 매핑을 반환한다.

    봇을 초대한 채널만 조회 대상으로 삼는다. 슬랙 API도 미참여 채널을 막지만,
    여기서 먼저 걸러야 어느 채널을 볼 수 있는지 에이전트에게 알려줄 수 있다.
    """
    if "channels" in _channels_cache:
        return _channels_cache["channels"]

    response = await slack_client.users_conversations(
        types="public_channel,private_channel", limit=200, exclude_archived=True
    )
    channels = {c["id"]: c.get("name", c["id"]) for c in response.get("channels", [])}

    _channels_cache["channels"] = channels
    return channels


def _readable(text: str, name_by_id: dict[str, str]) -> str:
    """<@U123> 형태의 멘션을 실명으로 바꾼다."""
    return _MENTION_RE.sub(lambda m: f"@{name_by_id.get(m.group(1), m.group(1))}", text)


def _permalink(workspace: str, channel: str, ts: str) -> str:
    """chat.getPermalink 왕복 대신 URL을 직접 조립한다."""
    return f"https://{workspace}.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _format_message(
    message: dict,
    channel: str,
    workspace: str,
    name_by_id: dict[str, str],
    is_reply: bool = False,
) -> str:
    author = name_by_id.get(message.get("user", ""), "Bot")
    when = datetime.fromtimestamp(float(message["ts"]), tz=KST).strftime(
        "%Y-%m-%d %H:%M"
    )
    body = _readable(message.get("text", ""), name_by_id)
    prefix = "  ↳ " if is_reply else "- "
    link = "" if is_reply else f" {_permalink(workspace, channel, message['ts'])}"
    return f"{prefix}[{when}] {author}: {body}{link}"


def get_search_channel_messages_tool(
    slack_client, workspace: str, default_channel: str
):
    """
    채널 대화를 찾는 도구를 반환한다.

    Args:
        slack_client: Slack 클라이언트
        workspace: 슬랙 워크스페이스 도메인 (permalink 조립용)
        default_channel: channel 인자가 없을 때 사용할 현재 채널
    """

    @tool
    async def search_channel_messages(
        query: Annotated[str, "찾을 내용. 정규식을 쓸 수 있습니다. 예: '연수|워크숍'"],
        channel: Annotated[
            str | None,
            "채널 ID 또는 채널명(#없이도 됨). 생략하면 지금 대화 중인 채널을 찾습니다.",
        ] = None,
        days: Annotated[int, "며칠 전까지 거슬러 볼지"] = 30,
    ) -> str:
        """
        슬랙 채널의 과거 대화를 찾습니다.

        슬랙은 봇 토큰에 검색 API를 열어주지 않기 때문에, 채널을 지정해 최근
        이력을 가져온 뒤 직접 걸러냅니다. 따라서 채널을 특정할 수 있어야 합니다.

        볼 수 있는 채널은 봇이 초대된 채널뿐입니다. 다른 채널을 지정하면 볼 수 있는
        채널 목록을 알려주므로, 어느 채널인지 모르면 그것으로 확인하세요.

        최상위 메시지에서 후보를 찾은 뒤 그 스레드의 답글까지 읽습니다.
        최상위에서 아무것도 못 찾으면 최근 스레드 몇 개를 대신 훑어봅니다.

        Returns:
            str: 일치한 메시지와 링크
        """
        try:
            regex = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return f"검색어를 정규식으로 해석할 수 없습니다: {e}"

        if channel:
            try:
                channels = await _bot_channels(slack_client)
            except SlackApiError as e:
                return f"봇이 참여한 채널 목록을 가져오지 못했습니다: {e}"

            wanted = channel.lstrip("#")
            by_name = {name: cid for cid, name in channels.items()}
            target_channel = wanted if wanted in channels else by_name.get(wanted)

            if target_channel is None:
                allowed = ", ".join(f"#{n}" for n in sorted(channels.values()))
                return (
                    f"'{channel}'은(는) 봇이 들어가 있지 않은 채널이라 볼 수 없습니다. "
                    f"볼 수 있는 채널: {allowed or '없음'}. "
                    "다른 채널을 봐야 하면 그 채널에 봇을 초대해 달라고 안내하세요."
                )
        else:
            target_channel = default_channel

        oldest = (datetime.now(tz=KST) - timedelta(days=days)).timestamp()

        try:
            response = await slack_client.conversations_history(
                channel=target_channel, oldest=str(oldest), limit=MAX_HISTORY
            )
            messages = response.get("messages", [])

            user_info = await slack_users_list(slack_client)
            name_by_id = {
                member["id"]: member.get("real_name", "Unknown")
                for member in user_info["members"]
            }

            matched = [m for m in messages if regex.search(m.get("text", ""))]
            fallback_used = False

            # 최상위에 없더라도 스레드 안에 있을 수 있다. 그때는 최근 스레드를 훑는다.
            if not matched:
                matched = [m for m in messages if m.get("reply_count")]
                fallback_used = True

            candidates = matched[:MAX_THREADS]

            lines: list[str] = []
            for parent in candidates:
                parent_line = _format_message(
                    parent, target_channel, workspace, name_by_id
                )
                reply_lines: list[str] = []

                if parent.get("reply_count"):
                    replies = await slack_client.conversations_replies(
                        channel=target_channel, ts=parent["ts"]
                    )
                    reply_lines = [
                        _format_message(
                            reply, target_channel, workspace, name_by_id, is_reply=True
                        )
                        for reply in replies.get("messages", [])[1:]
                        if regex.search(reply.get("text", ""))
                    ]

                # 폴백으로 훑는 중이면 답글에서 걸린 스레드만 남긴다
                if fallback_used and not reply_lines:
                    continue

                lines.append(parent_line)
                lines.extend(reply_lines)
        except SlackApiError as e:
            return (
                f"채널 대화 조회 실패: {e}. 채널 ID가 맞는지, "
                "봇이 그 채널에 들어가 있는지 확인이 필요합니다."
            )

        if not lines:
            return (
                f"최근 {days}일 안에서 '{query}'와 일치하는 대화를 찾지 못했습니다. "
                "기간을 늘리거나 다른 검색어로 다시 시도해 보세요."
            )

        if len(lines) > MAX_MESSAGES:
            omitted = len(lines) - MAX_MESSAGES
            lines = lines[:MAX_MESSAGES] + [
                f"[...{omitted}건 생략됨. 검색어를 좁히세요.]"
            ]

        header = (
            f"최근 {days}일 대화에서 찾은 결과"
            if not fallback_used
            else "최상위 메시지에는 없어 최근 스레드 안을 훑은 결과"
        )
        return f"{header}:\n" + "\n".join(lines)

    return search_channel_messages
