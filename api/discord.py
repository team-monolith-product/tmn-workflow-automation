"""
Discord API 래퍼 함수
"""

import os

import requests

BASE_URL = "https://discord.com/api/v10"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}",
        "Content-Type": "application/json",
    }


def get_guild_channels(guild_id: str) -> list[dict]:
    """서버의 모든 채널 목록 조회"""
    resp = requests.get(f"{BASE_URL}/guilds/{guild_id}/channels", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def find_forum_channel(guild_id: str, channel_name: str) -> dict | None:
    """
    서버에서 이름이 일치하는 포럼 채널을 찾는다.

    Args:
        guild_id: Discord 서버 ID
        channel_name: 찾을 채널 이름 (예: "선인고등학교-공지")

    Returns:
        채널 dict 또는 None
    """
    channels = get_guild_channels(guild_id)
    for ch in channels:
        # type 15 = Forum channel
        if ch.get("type") == 15 and ch.get("name") == channel_name:
            return ch
    return None


def fetch_message(channel_id: str, message_id: str) -> dict:
    """특정 채널의 메시지를 가져온다."""
    resp = requests.get(
        f"{BASE_URL}/channels/{channel_id}/messages/{message_id}",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json()


def get_channel(channel_id: str) -> dict:
    """채널 정보를 가져온다."""
    resp = requests.get(f"{BASE_URL}/channels/{channel_id}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def fetch_thread_template(thread_id: str) -> dict:
    """
    포럼 스레드(게시물)의 제목과 본문을 가져온다.

    Returns:
        {"title": "yymmdd_간식증빙사진", "content": "-"}
    """
    channel = get_channel(thread_id)
    title = channel.get("name", "")

    # 스레드의 첫 메시지 가져오기 (thread_id == channel_id에서 가장 오래된 메시지)
    resp = requests.get(
        f"{BASE_URL}/channels/{thread_id}/messages",
        headers=_headers(),
        params={"limit": 1, "sort_order": "asc"},
    )
    resp.raise_for_status()
    messages = resp.json()
    content = messages[0].get("content", "") if messages else ""

    return {"title": title, "content": content}


def get_active_threads(guild_id: str) -> list[dict]:
    """서버의 활성 스레드 목록 조회"""
    resp = requests.get(
        f"{BASE_URL}/guilds/{guild_id}/threads/active",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("threads", [])


def get_recent_threads_in_forum(guild_id: str, forum_channel_id: str) -> list[dict]:
    """
    포럼 채널의 최근 스레드(게시물) 목록을 가져온다.
    활성 스레드 중 해당 포럼에 속한 것만 필터링.
    """
    threads = get_active_threads(guild_id)
    return [t for t in threads if t.get("parent_id") == forum_channel_id]


def create_forum_thread(
    channel_id: str, title: str, content: str
) -> dict:
    """
    포럼 채널에 새 게시물(스레드)을 생성한다.

    Args:
        channel_id: 포럼 채널 ID
        title: 게시물 제목
        content: 게시물 본문

    Returns:
        생성된 스레드 dict
    """
    payload = {
        "name": title,
        "message": {"content": content},
    }
    resp = requests.post(
        f"{BASE_URL}/channels/{channel_id}/threads",
        headers=_headers(),
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def send_message(channel_id: str, content: str) -> dict:
    """일반 채널에 메시지를 보낸다."""
    payload = {"content": content}
    resp = requests.post(
        f"{BASE_URL}/channels/{channel_id}/messages",
        headers=_headers(),
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()
