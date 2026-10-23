"""
Discord API 래퍼 함수
"""

import os

import requests

BASE_URL = "https://discord.com/api/v10"
REQUEST_TIMEOUT = 10


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}",
        "Content-Type": "application/json",
    }


def get_guild_channels(guild_id: str) -> list[dict]:
    """서버의 모든 채널 목록 조회"""
    resp = requests.get(
        f"{BASE_URL}/guilds/{guild_id}/channels",
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_channel(channel_id: str) -> dict:
    """채널(스레드 포함) 정보 조회"""
    resp = requests.get(
        f"{BASE_URL}/channels/{channel_id}",
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_message(channel_id: str, message_id: str) -> dict:
    """특정 채널의 메시지 조회"""
    resp = requests.get(
        f"{BASE_URL}/channels/{channel_id}/messages/{message_id}",
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_channel_messages(
    channel_id: str, limit: int = 100, before: str | None = None
) -> list[dict]:
    """채널의 최근 메시지 목록 조회 (최신순). before 를 주면 그 메시지 이전부터"""
    params = {"limit": limit}
    if before:
        params["before"] = before
    resp = requests.get(
        f"{BASE_URL}/channels/{channel_id}/messages",
        headers=_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_active_threads(guild_id: str) -> dict:
    """서버의 활성 스레드 목록 조회"""
    resp = requests.get(
        f"{BASE_URL}/guilds/{guild_id}/threads/active",
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_public_archived_threads(channel_id: str, before: str | None = None) -> dict:
    """채널의 보관된 공개 스레드 목록 조회 (최근 보관순). before 는 ISO8601 시각"""
    params = {"limit": 100}
    if before:
        params["before"] = before
    resp = requests.get(
        f"{BASE_URL}/channels/{channel_id}/threads/archived/public",
        headers=_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_attachment(url: str) -> bytes:
    """첨부 파일 내려받기. CDN 주소는 서명돼 있어 인증 헤더가 필요 없다"""
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def create_thread(channel_id: str, name: str, content: str) -> dict:
    """포럼 채널에 새 게시물(스레드) 생성"""
    payload = {
        "name": name,
        "message": {"content": content},
    }
    resp = requests.post(
        f"{BASE_URL}/channels/{channel_id}/threads",
        headers=_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def create_message(channel_id: str, content: str) -> dict:
    """채널에 메시지 생성"""
    payload = {"content": content}
    resp = requests.post(
        f"{BASE_URL}/channels/{channel_id}/messages",
        headers=_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
