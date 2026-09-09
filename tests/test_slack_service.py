"""Slack 사용자 목록의 페이지네이션과 공용 캐시를 검증합니다."""

from unittest.mock import AsyncMock, call

import pytest
from cachetools import TTLCache
from slack_sdk.errors import SlackApiError

from service import slack


@pytest.fixture
def cache_clock(monkeypatch):
    now = [0]
    monkeypatch.setattr(
        slack,
        "_cache_slack_users",
        TTLCache(maxsize=1, ttl=slack._cache_slack_users.ttl, timer=lambda: now[0]),
    )
    return now


async def test_전체_페이지를_합치고_다른_호출부에서도_캐시를_재사용한다(cache_clock):
    first = {"id": "U01", "profile": {"email": "first@example.com"}}
    second = {"id": "U02", "profile": {"email": "second@example.com"}}
    bot = {"id": "UBOT", "profile": None}
    client = AsyncMock()
    client.users_list.side_effect = [
        {"members": [first, bot], "response_metadata": {"next_cursor": "page2"}},
        {"members": [second], "response_metadata": {"next_cursor": ""}},
    ]

    result = await slack.slack_users_list(client)
    other_client = AsyncMock()
    emails = await slack.get_email_to_user_id_async(other_client)

    assert result["members"] == [first, bot, second]
    assert emails == {"first@example.com": "U01", "second@example.com": "U02"}
    assert client.users_list.await_args_list == [
        call(limit=200, cursor=None),
        call(limit=200, cursor="page2"),
    ]
    other_client.users_list.assert_not_awaited()


async def test_24시간_동안_재사용하고_만료되면_갱신한다(cache_clock):
    client = AsyncMock()
    client.users_list.side_effect = [
        {"members": [{"id": "U01"}]},
        {"members": [{"id": "U02"}]},
    ]
    original = await slack.slack_users_list(client)
    cache_clock[0] = 86400 - 1
    assert await slack.slack_users_list(client) is original
    client.users_list.assert_awaited_once()

    cache_clock[0] = 86400
    assert (await slack.slack_users_list(client))["members"] == [{"id": "U02"}]
    assert client.users_list.await_count == 2


async def test_중간_페이지_실패시_불완전한_목록을_캐시하지_않는다(cache_clock):
    client = AsyncMock()
    client.users_list.side_effect = [
        {"members": [{"id": "U01"}], "response_metadata": {"next_cursor": "page2"}},
        SlackApiError("ratelimited", {"error": "ratelimited"}),
        {"members": [{"id": "U01"}, {"id": "U02"}]},
    ]

    with pytest.raises(SlackApiError):
        await slack.slack_users_list(client)
    assert (await slack.slack_users_list(client))["members"] == [
        {"id": "U01"},
        {"id": "U02"},
    ]
    assert client.users_list.await_args == call(limit=200, cursor=None)
