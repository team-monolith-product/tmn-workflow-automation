"""에이전트 답변을 슬랙 블록에 담는 경로 테스트"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from app import common
from service.llm import extract_text

# Responses API 응답. reasoning 블록이 앞에 붙고 본문은 text 블록에 담긴다.
RESPONSES_CONTENT = [
    {"id": "rs_1", "summary": [], "type": "reasoning"},
    {"type": "text", "text": "부산 연수는 벡스코입니다.", "annotations": []},
]


class TestExtractText:
    def test_plain_string(self):
        assert extract_text("그냥 문자열") == "그냥 문자열"

    def test_skips_reasoning_block(self):
        assert extract_text(RESPONSES_CONTENT) == "부산 연수는 벡스코입니다."

    def test_joins_multiple_text_blocks(self):
        content = [
            {"type": "text", "text": "앞"},
            {"type": "reasoning", "summary": []},
            {"type": "text", "text": "뒤"},
        ]
        assert extract_text(content) == "앞뒤"


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.conversations_replies = AsyncMock(
        return_value={"messages": [{"user": "U1", "text": "부산 연수 정보 찾아줘"}]}
    )
    client.users_list = AsyncMock(
        return_value={"members": [{"id": "U1", "real_name": "배영빈", "profile": {}}]}
    )
    return client


class TestAnswer:
    async def test_posts_text_block_as_string(self, slack_client):
        """Responses API의 블록 리스트를 그대로 보내면 슬랙이 invalid_blocks로 거부한다"""
        executor = MagicMock()
        executor.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content=RESPONSES_CONTENT)]}
        )
        say = AsyncMock()

        with patch("app.common.ChatOpenAI"), patch(
            "app.common.create_react_agent", return_value=executor
        ):
            await common.answer(
                "1786006902.904459",
                "C0AP8CG1Y6N",
                "U1",
                "부산 연수 정보 찾아줘",
                say,
                slack_client,
                [],
            )

        text = say.call_args.args[0]["blocks"][0]["text"]["text"]
        assert text == "부산 연수는 벡스코입니다."
