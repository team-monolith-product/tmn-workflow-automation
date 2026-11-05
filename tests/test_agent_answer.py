"""에이전트 답변을 슬랙 블록에 담는 경로 테스트"""

import unicodedata
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

    async def test_empty_answer_falls_back_to_placeholder(self, slack_client):
        """reasoning 블록만 있어 본문이 비면 빈 문자열 대신 안내 문구를 보낸다 (Slack invalid_blocks 방지)"""
        executor = MagicMock()
        executor.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    AIMessage(
                        content=[{"id": "rs_1", "summary": [], "type": "reasoning"}]
                    )
                ]
            }
        )
        say = AsyncMock()

        with patch("app.common.ChatOpenAI"), patch(
            "app.common.create_react_agent", return_value=executor
        ):
            await common.answer(
                "1786006902.904459",
                "C0AP8CG1Y6N",
                "U1",
                "질문",
                say,
                slack_client,
                [],
            )

        text = say.call_args.args[0]["blocks"][0]["text"]["text"]
        assert text

    async def test_long_answer_splits_into_chunks(self, slack_client):
        """3000자를 넘는 응답은 여러 블록으로 나눠 보낸다 (Slack invalid_blocks 방지)"""
        long_text = "가" * 7000
        executor = MagicMock()
        executor.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content=long_text)]}
        )
        say = AsyncMock()

        with patch("app.common.ChatOpenAI"), patch(
            "app.common.create_react_agent", return_value=executor
        ):
            await common.answer(
                "1786006902.904459",
                "C0AP8CG1Y6N",
                "U1",
                "질문",
                say,
                slack_client,
                [],
            )

        assert say.call_count == 3
        for call in say.call_args_list:
            text = call.args[0]["blocks"][0]["text"]["text"]
            assert len(text) <= 3000

    async def test_appends_instructions_to_system_prompt(self, slack_client):
        """봇별 지침은 시스템 프롬프트 끝에 붙고, 주지 않으면 프롬프트가 그대로다"""
        executor = MagicMock()
        executor.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content="답")]}
        )

        async def system_prompt(*instructions):
            with patch("app.common.ChatOpenAI"), patch(
                "app.common.create_react_agent", return_value=executor
            ):
                await common.answer(
                    "1786006902.904459",
                    "C0AP8CG1Y6N",
                    "U1",
                    "질문",
                    AsyncMock(),
                    slack_client,
                    [],
                    *instructions,
                )
            return executor.ainvoke.call_args.args[0]["messages"][0].content

        default = await system_prompt()
        with_instructions = await system_prompt("\n\n데이터 지침")

        assert default.endswith("(백틱 3개)")
        assert with_instructions == default + "\n\n데이터 지침"


class TestThreadFiles:
    """메시지 text 에는 파일이 없어서, 에이전트가 "이 파일" 을 고르려면 이름이 필요하다"""

    async def _human_message(self, slack_client, messages, user="U1"):
        slack_client.conversations_replies = AsyncMock(
            return_value={"messages": messages}
        )
        executor = MagicMock()
        executor.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content="답")]}
        )
        with patch("app.common.ChatOpenAI"), patch(
            "app.common.create_react_agent", return_value=executor
        ):
            await common.answer(
                "1793846401.327239",
                "C0CE8EQ3XK5",
                user,
                messages[-1]["text"],
                AsyncMock(),
                slack_client,
                [],
            )
        return executor.ainvoke.call_args.args[0]["messages"][1].content

    async def test_질문_메시지에_올린_파일을_그_질문에_붙인다(self, slack_client):
        content = await self._human_message(
            slack_client,
            [
                {
                    "user": "U1",
                    "text": "이거 메일로보내",
                    "files": [{"name": "IMG_5467-2 (2).heic"}],
                },
                {
                    "user": "U1",
                    "text": "감사합니다 를 메일로 보내 이파일과 함께",
                    "files": [{"name": "1.codap (3).json"}],
                },
            ],
        )

        assert "이거 메일로보내\n[첨부 파일: IMG_5467-2 (2).heic]" in content
        assert (
            "감사합니다 를 메일로 보내 이파일과 함께\n[첨부 파일: 1.codap (3).json]"
            in content
        )

    async def test_이름_없는_파일과_파일_없는_메시지에는_줄을_붙이지_않는다(
        self, slack_client
    ):
        content = await self._human_message(
            slack_client,
            [
                {"user": "U1", "text": "지운 파일", "files": [{"id": "F1"}]},
                {"user": "U1", "text": "질문"},
            ],
        )

        assert "첨부 파일" not in content

    async def test_마지막_메시지가_질문한_사람_것이_아니면_그_파일을_붙이지_않는다(
        self, slack_client
    ):
        """[수정] 처럼 질문이 스레드 메시지가 아닐 때 남의 파일을 질문에 붙이지 않는다"""
        content = await self._human_message(
            slack_client,
            [{"text": "초안 카드", "files": [{"name": "남의 파일.pdf"}]}],
        )

        assert "남의 파일.pdf" not in content


async def test_대화에_보여주는_파일_이름은_완성형이다():
    """맥에서 올린 NFD 이름을 그대로 보여주면 에이전트가 이름을 옮겨 쓰다 어긋난다"""
    nfd = unicodedata.normalize("NFD", "가이드.pdf")

    assert common._files_note({"files": [{"name": nfd}]}) == "\n[첨부 파일: 가이드.pdf]"
