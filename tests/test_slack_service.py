"""service/slack.py 단위 테스트입니다."""

from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from service.slack import get_user_id_by_email_async


async def test_이메일로_슬랙_사용자_id를_조회한다():
    client = AsyncMock()
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}

    user_id = await get_user_id_by_email_async(client, "operator@team-mono.com")

    assert user_id == "U01OWNER"
    client.users_lookupByEmail.assert_awaited_once_with(email="operator@team-mono.com")


async def test_워크스페이스에_없는_이메일은_None을_반환한다():
    client = AsyncMock()
    client.users_lookupByEmail.side_effect = SlackApiError(
        message="users_not_found",
        response={"error": "users_not_found"},
    )

    user_id = await get_user_id_by_email_async(client, "unknown@team-mono.com")

    assert user_id is None


async def test_users_not_found가_아닌_오류는_그대로_전파한다():
    client = AsyncMock()
    client.users_lookupByEmail.side_effect = SlackApiError(
        message="ratelimited",
        response={"error": "ratelimited"},
    )

    with pytest.raises(SlackApiError):
        await get_user_id_by_email_async(client, "operator@team-mono.com")
