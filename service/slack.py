"""
Slack API를 활용하는 Service Layer입니다.
"""

import time
from typing import Any
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


def find_thread_ts_by_text(
    slack_client: WebClient,
    channel_id: str,
    search_texts: list[str],
    hours: int = 12,
) -> dict[str, str]:
    """
    채널에서 최근 N시간 이내 메시지 중 search_texts 항목을 포함한 메시지의 ts를 찾는다.

    Args:
        slack_client: Slack WebClient
        channel_id: 검색 대상 채널 ID
        search_texts: 메시지 본문에 포함되어야 할 텍스트 목록
        hours: 검색 시간 범위 (시간 단위)

    Returns:
        dict[str, str]: search_text -> thread_ts 매핑 (못 찾은 항목은 키 없음)
    """
    oldest = time.time() - 3600 * hours
    response = slack_client.conversations_history(
        channel=channel_id,
        oldest=str(int(oldest)),
        limit=200,
    )

    found: dict[str, str] = {}
    for message in response.get("messages", []):
        text = message.get("text", "")
        for search_text in search_texts:
            if search_text in found:
                continue
            if search_text in text:
                found[search_text] = message["ts"]
    return found


def get_email_to_user_id(slack_client: WebClient) -> dict[str, str]:
    """
    Args:
        slack_client (WebClient): Slack WebClient
    Returns:
        dict[str, str]: 이메일과 Slack User ID 매핑
    """
    email_to_user_id = {}
    cursor = None

    while True:
        response = slack_client.users_list(cursor=cursor)
        members = response["members"]

        for member in members:
            profile = member.get("profile", {})
            email = profile.get("email")
            if email:
                email_to_user_id[email] = member["id"]

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return email_to_user_id


async def get_email_to_user_id_async(slack_client: AsyncWebClient) -> dict[str, str]:
    """
    Args:
        slack_client (WebClient): Slack WebClient
    Returns:
        dict[str, str]: 이메일과 Slack User ID 매핑
    """
    email_to_user_id = {}
    cursor = None

    while True:
        response = await slack_client.users_list(cursor=cursor)
        members = response["members"]

        for member in members:
            profile = member.get("profile", {})
            email = profile.get("email")
            if email:
                email_to_user_id[email] = member["id"]

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return email_to_user_id


async def get_user_id_by_email_async(
    slack_client: AsyncWebClient, email: str
) -> str | None:
    """
    이메일 하나만 필요한 경우 워크스페이스 전체를 페이지네이션하는
    `get_email_to_user_id_async` 대신 사용합니다. `users.list` 반복 호출은
    워크스페이스 규모에 비례해 요청 수가 늘어나 rate limit(429)에 걸리기 쉽습니다.

    Args:
        slack_client (AsyncWebClient): Slack WebClient
        email (str): 조회할 이메일
    Returns:
        str | None: 이메일에 해당하는 Slack User ID (없으면 None)
    """
    try:
        response = await slack_client.users_lookupByEmail(email=email)
    except SlackApiError as e:
        if e.response.get("error") == "users_not_found":
            return None
        raise
    return response["user"]["id"]


def get_user_id_to_user_info(
    slack_client: WebClient,
    user_ids: list[str],
) -> dict[str, Any]:
    """
    Args:
        slack_client (WebClient): Slack WebClient
        user_ids (list[str]): 사용자 ID 목록
    Returns:
        dict[str, Any]: 사용자 ID와 사용자 정보 매핑
    """
    return {
        user_id: slack_client.users_info(user=user_id)["user"] for user_id in user_ids
    }


async def get_user_id_to_user_info_async(
    slack_client: AsyncWebClient,
    user_ids: list[str],
) -> dict[str, Any]:
    """
    Args:
        slack_client (AsyncWebClient): Slack WebClient
        user_ids (list[str]): 사용자 ID 목록
    Returns:
        dict[str, Any]: 사용자 ID와 사용자 정보 매핑
    """
    return {
        user_id: (await slack_client.users_info(user=user_id))["user"]
        for user_id in user_ids
    }
