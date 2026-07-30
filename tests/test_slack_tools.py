"""
슬랙 대화 조회 도구 테스트

핵심은 2단계다. conversations.history는 최상위 메시지만 주므로,
후보를 좁힌 뒤 그 스레드만 conversations.replies로 읽어야 한다.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.tools import slack_tools

USERS = {
    "members": [
        {"id": "U1", "real_name": "배영빈"},
        {"id": "U2", "real_name": "김담당"},
    ]
}


def _msg(ts: str, user: str, text: str, reply_count: int = 0) -> dict:
    message = {"ts": ts, "user": user, "text": text}
    if reply_count:
        message["reply_count"] = reply_count
    return message


def _tool(client):
    slack_tools._channels_cache.clear()
    return slack_tools.get_search_channel_messages_tool(client, "monolith", "C_CUR")


BOT_CHANNELS = {
    "channels": [{"id": "C_OPS", "name": "운영"}, {"id": "C_CUR", "name": "현재"}]
}


class TestSearchChannelMessages:
    @pytest.mark.asyncio
    async def test_reads_replies_only_for_matched_threads(self):
        """최상위에서 걸린 스레드만 답글을 읽는다 (메시지마다 부르지 않는다)"""
        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [
                _msg("1700000001.0", "U1", "연수 준비 논의", reply_count=2),
                _msg("1700000002.0", "U2", "점심 뭐 먹지", reply_count=5),
            ]
        }
        client.conversations_replies.return_value = {
            "messages": [
                _msg("1700000001.0", "U1", "연수 준비 논의"),
                _msg("1700000003.0", "U2", "연수 간식은 제가 삽니다"),
            ]
        }

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            result = await _tool(client).ainvoke({"query": "연수"})

        # 걸린 스레드 하나만 replies 호출
        client.conversations_replies.assert_called_once()
        assert client.conversations_replies.call_args.kwargs["ts"] == "1700000001.0"
        assert "연수 준비 논의" in result
        assert "간식은 제가 삽니다" in result
        assert "점심" not in result

    @pytest.mark.asyncio
    async def test_falls_back_to_threads_when_no_top_level_match(self):
        """최상위에 없으면 최근 스레드 안을 훑고, 답글에서 걸린 것만 남긴다"""
        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [
                _msg("1700000001.0", "U1", "이번 주 회의", reply_count=2),
                _msg("1700000002.0", "U2", "공지", reply_count=0),
            ]
        }
        client.conversations_replies.return_value = {
            "messages": [
                _msg("1700000001.0", "U1", "이번 주 회의"),
                _msg("1700000003.0", "U2", "영수증은 제가 정리할게요"),
            ]
        }

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            result = await _tool(client).ainvoke({"query": "영수증"})

        assert "최상위 메시지에는 없어" in result
        assert "영수증은 제가 정리할게요" in result

    @pytest.mark.asyncio
    async def test_uses_current_channel_by_default(self):
        """channel을 안 주면 지금 대화 중인 채널을 본다"""
        client = AsyncMock()
        client.conversations_history.return_value = {"messages": []}

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            await _tool(client).ainvoke({"query": "무엇"})

        assert client.conversations_history.call_args.kwargs["channel"] == "C_CUR"

    @pytest.mark.asyncio
    async def test_mentions_are_replaced_with_real_names(self):
        """<@U1> 같은 멘션은 실명으로 바꿔서 넘긴다"""
        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [_msg("1700000001.0", "U2", "<@U1> 연수 확인 부탁해요")]
        }

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            result = await _tool(client).ainvoke({"query": "연수"})

        assert "@배영빈" in result
        assert "<@U1>" not in result

    @pytest.mark.asyncio
    async def test_invalid_regex_is_reported(self):
        """잘못된 정규식은 조회 전에 알려준다"""
        client = AsyncMock()

        result = await _tool(client).ainvoke({"query": "연수("})

        client.conversations_history.assert_not_called()
        assert "정규식" in result

    @pytest.mark.asyncio
    async def test_no_match_guides_retry(self):
        """못 찾으면 범위를 넓히라고 안내한다"""
        client = AsyncMock()
        client.conversations_history.return_value = {
            "messages": [_msg("1700000001.0", "U1", "관계없는 이야기")]
        }

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            result = await _tool(client).ainvoke({"query": "연수"})

        assert "찾지 못했습니다" in result


class TestChannelRestriction:
    """봇이 초대된 채널만 볼 수 있다"""

    @pytest.mark.asyncio
    async def test_channel_not_joined_is_refused_with_allowed_list(self):
        """미참여 채널은 조회하지 않고 볼 수 있는 채널을 알려준다"""
        client = AsyncMock()
        client.users_conversations.return_value = BOT_CHANNELS

        result = await _tool(client).ainvoke({"query": "연수", "channel": "C_SECRET"})

        client.conversations_history.assert_not_called()
        assert "들어가 있지 않은 채널" in result
        assert "#운영" in result

    @pytest.mark.asyncio
    async def test_joined_channel_id_is_allowed(self):
        client = AsyncMock()
        client.users_conversations.return_value = BOT_CHANNELS
        client.conversations_history.return_value = {"messages": []}

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            await _tool(client).ainvoke({"query": "연수", "channel": "C_OPS"})

        assert client.conversations_history.call_args.kwargs["channel"] == "C_OPS"

    @pytest.mark.asyncio
    async def test_channel_name_is_resolved_to_id(self):
        """채널명으로 줘도 ID로 바꿔 조회한다 (#접두사 허용)"""
        client = AsyncMock()
        client.users_conversations.return_value = BOT_CHANNELS
        client.conversations_history.return_value = {"messages": []}

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            await _tool(client).ainvoke({"query": "연수", "channel": "#운영"})

        assert client.conversations_history.call_args.kwargs["channel"] == "C_OPS"

    @pytest.mark.asyncio
    async def test_omitted_channel_skips_membership_lookup(self):
        """현재 채널을 쓸 때는 목록 조회를 하지 않는다"""
        client = AsyncMock()
        client.conversations_history.return_value = {"messages": []}

        with patch.object(
            slack_tools, "slack_users_list", AsyncMock(return_value=USERS)
        ):
            await _tool(client).ainvoke({"query": "연수"})

        client.users_conversations.assert_not_called()
        assert client.conversations_history.call_args.kwargs["channel"] == "C_CUR"
