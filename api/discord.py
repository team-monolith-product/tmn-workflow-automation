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


def get_channel(channel_id: str) -> dict:
    """채널(스레드 포함) 정보 조회"""
    resp = requests.get(f"{BASE_URL}/channels/{channel_id}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_message(channel_id: str, message_id: str) -> dict:
    """특정 채널의 메시지 조회"""
    resp = requests.get(
        f"{BASE_URL}/channels/{channel_id}/messages/{message_id}",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json()


def get_active_threads(guild_id: str) -> dict:
    """서버의 활성 스레드 목록 조회"""
    resp = requests.get(
        f"{BASE_URL}/guilds/{guild_id}/threads/active",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json()


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
    )
    resp.raise_for_status()
    return resp.json()
