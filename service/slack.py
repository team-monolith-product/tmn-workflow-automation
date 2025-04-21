"""
Slack API를 활용하는 Service Layer입니다.
"""

from typing import Any, Dict, List
from slack_sdk import WebClient
from slack_sdk.web.async_client import AsyncWebClient


def get_email_to_user_id(slack_client: WebClient) -> Dict[str, str]:
    """
    Args:
        slack_client (WebClient): Slack WebClient
    Returns:
        Dict[str, str]: 이메일과 Slack User ID 매핑
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


async def get_email_to_user_id_async(slack_client: AsyncWebClient) -> Dict[str, str]:
    """
    Args:
        slack_client (WebClient): Slack WebClient
    Returns:
        Dict[str, str]: 이메일과 Slack User ID 매핑
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


def get_user_id_to_user_info(
    slack_client: WebClient,
    user_ids: List[str],
) -> Dict[str, Any]:
    """
    Args:
        slack_client (WebClient): Slack WebClient
        user_ids (List[str]): 사용자 ID 목록
    Returns:
        Dict[str, Any]: 사용자 ID와 사용자 정보 매핑
    """
    return {
        user_id: slack_client.users_info(user=user_id)["user"] for user_id in user_ids
    }


async def get_user_id_to_user_info_async(
    slack_client: AsyncWebClient,
    user_ids: List[str],
) -> Dict[str, Any]:
    """
    Args:
        slack_client (AsyncWebClient): Slack WebClient
        user_ids (List[str]): 사용자 ID 목록
    Returns:
        Dict[str, Any]: 사용자 ID와 사용자 정보 매핑
    """
    return {
        user_id: (await slack_client.users_info(user=user_id))["user"]
        for user_id in user_ids
    }
