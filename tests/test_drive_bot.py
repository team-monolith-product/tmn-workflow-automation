"""
Drive Bot 채널 캔버스 주입 테스트
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from slack_sdk.errors import SlackApiError

from app import common, drive_bot

CANVAS_FILE = {
    "id": "F123",
    "title": "CLAUDE.md",
    "filetype": "canvas",
    "url_private_download": "https://files.slack.com/canvas-1",
}


class TestFetchChannelCanvas:
    """채널 캔버스 조회"""

    @pytest.mark.asyncio
    async def test_returns_canvas_body(self):
        """캔버스가 있으면 본문을 돌려준다"""
        client = AsyncMock()
        client.files_list.return_value = {"files": [CANVAS_FILE]}

        response = AsyncMock()
        response.read.return_value = "# 채널 규칙\n- 자료는 X 폴더".encode("utf-8")
        response.raise_for_status = MagicMock()

        # session.get()은 코루틴이 아니라 async context manager를 돌려준다
        get_cm = AsyncMock()
        get_cm.__aenter__.return_value = response
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = session

        with patch.object(
            common.aiohttp, "ClientSession", MagicMock(return_value=session_cm)
        ):
            result = await common.fetch_channel_canvas(client, "C1", "xoxb-token")

        session.get.assert_called_once_with(
            CANVAS_FILE["url_private_download"],
            headers={"Authorization": "Bearer xoxb-token"},
        )

        client.files_list.assert_called_once_with(
            channel="C1", types="canvas", count=100
        )
        assert "채널 규칙" in result

    @pytest.mark.asyncio
    async def test_no_canvas_returns_none(self):
        """캔버스가 없으면 None"""
        client = AsyncMock()
        client.files_list.return_value = {"files": []}

        assert await common.fetch_channel_canvas(client, "C1", "xoxb-token") is None

    @pytest.mark.asyncio
    async def test_slack_error_does_not_break_bot(self):
        """스코프 누락 등으로 실패해도 None을 돌려주고 봇은 계속 동작한다"""
        client = AsyncMock()
        client.files_list.side_effect = SlackApiError(
            "missing_scope", {"error": "missing_scope"}
        )

        assert await common.fetch_channel_canvas(client, "C1", "xoxb-token") is None


class TestSystemPromptInjection:
    """캔버스의 시스템 프롬프트 주입"""

    def test_canvas_section_absent_without_canvas(self):
        """캔버스가 없으면 해당 섹션 자체가 없다"""
        prompt = drive_bot._build_system_prompt(None)
        assert "이 채널의 공통 맥락" not in prompt

    def test_canvas_body_is_injected(self):
        """캔버스가 있으면 본문이 프롬프트에 들어간다"""
        prompt = drive_bot._build_system_prompt("# 채널 규칙\n- 자료는 X 폴더")

        assert "이 채널의 공통 맥락" in prompt
        assert "자료는 X 폴더" in prompt

    def test_base_instructions_are_kept(self):
        """캔버스가 붙어도 기본 지침은 그대로 유지된다"""
        prompt = drive_bot._build_system_prompt("아무 내용")

        assert "search_drive_files" in prompt
        assert "create_ops_task" in prompt


class TestPickBotCanvas:
    """채널에 캔버스가 여럿일 때 봇 기준 캔버스를 고른다"""

    def test_picks_canvas_named_claude_md(self):
        """이름이 CLAUDE.md인 캔버스를 고른다"""
        files = [
            {"id": "F1", "title": "7월 회의록"},
            {"id": "F2", "title": "CLAUDE.md"},
        ]
        assert common._pick_bot_canvas(files)["id"] == "F2"

    def test_name_match_is_case_insensitive(self):
        """대소문자가 달라도 인식한다"""
        files = [{"id": "F1", "title": "claude.md"}]
        assert common._pick_bot_canvas(files)["id"] == "F1"

    def test_falls_back_to_name_field(self):
        """title이 없으면 name을 본다"""
        files = [{"id": "F1", "name": "CLAUDE.md"}]
        assert common._pick_bot_canvas(files)["id"] == "F1"

    def test_single_canvas_is_used_even_without_matching_name(self):
        """캔버스가 하나뿐이면 이름이 달라도 쓴다"""
        files = [{"id": "F1", "title": "채널 안내"}]
        assert common._pick_bot_canvas(files)["id"] == "F1"

    def test_multiple_canvases_without_match_picks_none(self):
        """여럿인데 이름이 맞는 게 없으면 고르지 않는다 (엉뚱한 것 방지)"""
        files = [{"id": "F1", "title": "회의록"}, {"id": "F2", "title": "온보딩"}]
        assert common._pick_bot_canvas(files) is None

    def test_empty_list_returns_none(self):
        assert common._pick_bot_canvas([]) is None
