"""
노션 운영 DB 도구 테스트
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from pydantic import ValidationError

from app import common
from app.tools import notion_tools

# 실제 운영 DB 스키마 형태 (필요한 부분만)
OPS_PROPERTIES = {
    "이름": {"type": "title", "title": {}},
    "상태": {
        "type": "status",
        "status": {
            "options": [
                {"id": "opt-todo", "name": "시작 전"},
                {"id": "opt-doing", "name": "진행 중"},
                {"id": "opt-done", "name": "완료"},
            ],
            "groups": [
                {"id": "g1", "name": "To-do", "option_ids": ["opt-todo"]},
                {"id": "g2", "name": "In progress", "option_ids": ["opt-doing"]},
                {"id": "g3", "name": "Complete", "option_ids": ["opt-done"]},
            ],
        },
    },
    "유형": {
        "type": "select",
        "select": {"options": [{"name": "발송"}, {"name": "문서"}, {"name": "CS"}]},
    },
    "담당자": {
        "type": "multi_select",
        "multi_select": {"options": [{"name": "팀모노리스"}, {"name": "학회"}]},
    },
    "날짜": {"type": "date", "date": {}},
}

THREAD_URL = "https://monolith-keb2010.slack.com/archives/C123/p1700000000000000"


class TestDefaultStatusName:
    """기본 상태 선택 로직"""

    def test_picks_first_option_of_todo_group(self):
        """To-do 그룹의 첫 옵션을 고른다"""
        assert notion_tools._default_status_name(OPS_PROPERTIES["상태"]) == "시작 전"

    def test_option_order_does_not_matter(self):
        """옵션 배열 순서가 바뀌어도 To-do 그룹을 따라간다"""
        reordered = {
            "status": {
                "options": [
                    {"id": "opt-done", "name": "완료"},
                    {"id": "opt-todo", "name": "시작 전"},
                ],
                "groups": [
                    {"id": "g3", "name": "Complete", "option_ids": ["opt-done"]},
                    {"id": "g1", "name": "To-do", "option_ids": ["opt-todo"]},
                ],
            }
        }
        assert notion_tools._default_status_name(reordered) == "시작 전"

    def test_falls_back_to_first_option_without_groups(self):
        """그룹이 없으면 첫 옵션으로 떨어진다"""
        no_groups = {"status": {"options": [{"id": "a", "name": "대기"}]}}
        assert notion_tools._default_status_name(no_groups) == "대기"


class TestCreateOpsTaskTool:
    """도구 생성과 실행"""

    def _patched(self):
        """스키마 조회를 실제 운영 DB 형태로 대체한다"""
        return (
            patch.object(notion_tools, "get_data_source_id", return_value="ds-1"),
            patch.object(
                notion_tools,
                "get_data_source_schema",
                return_value={"properties": OPS_PROPERTIES},
            ),
        )

    def test_options_come_from_schema(self):
        """유형·담당자 선택지가 노션 스키마에서 온다"""
        p1, p2 = self._patched()
        with p1, p2:
            tool = notion_tools.get_create_ops_task_tool("db-1", THREAD_URL)

        schema_json = json.dumps(
            tool.args_schema.model_json_schema(), ensure_ascii=False
        )
        for option in ["발송", "문서", "CS", "팀모노리스", "학회"]:
            assert option in schema_json

    def test_invalid_option_is_rejected(self):
        """스키마에 없는 값은 도구 호출 전에 거부된다"""
        p1, p2 = self._patched()
        with p1, p2:
            tool = notion_tools.get_create_ops_task_tool("db-1", THREAD_URL)

        with pytest.raises(ValidationError):
            tool.args_schema(name="x", task_type="없는유형", assignees=["팀모노리스"])

        with pytest.raises(ValidationError):
            tool.args_schema(name="x", task_type="발송", assignees=["없는조직"])

    @pytest.mark.asyncio
    async def test_creates_page_with_expected_properties(self):
        """생성 요청에 이름·상태·유형·담당자가 스키마대로 담긴다"""
        p1, p2 = self._patched()
        with p1, p2:
            tool = notion_tools.get_create_ops_task_tool("db-1", THREAD_URL)

        mock_notion = MagicMock()
        mock_notion.pages.create.return_value = {
            "id": "page-1",
            "url": "https://notion.so/page-1",
        }

        with patch.object(notion_tools, "notion", mock_notion):
            result = await tool.ainvoke(
                {
                    "name": "8월 안내문 발송",
                    "task_type": "발송",
                    "assignees": ["팀모노리스", "학회"],
                    "date": "2026-08-01",
                }
            )

        kwargs = mock_notion.pages.create.call_args.kwargs
        assert kwargs["parent"] == {"data_source_id": "ds-1"}

        properties = kwargs["properties"]
        assert properties["이름"]["title"][0]["text"]["content"] == "8월 안내문 발송"
        assert properties["상태"]["status"]["name"] == "시작 전"
        assert properties["유형"]["select"]["name"] == "발송"
        assert properties["담당자"]["multi_select"] == [
            {"name": "팀모노리스"},
            {"name": "학회"},
        ]
        assert properties["날짜"]["date"]["start"] == "2026-08-01"
        assert result == "https://notion.so/page-1"

    @pytest.mark.asyncio
    async def test_date_is_omitted_when_not_given(self):
        """날짜를 넘기지 않으면 속성 자체를 보내지 않는다"""
        p1, p2 = self._patched()
        with p1, p2:
            tool = notion_tools.get_create_ops_task_tool("db-1", THREAD_URL)

        mock_notion = MagicMock()
        mock_notion.pages.create.return_value = {"id": "page-2", "url": "u"}

        with patch.object(notion_tools, "notion", mock_notion):
            await tool.ainvoke(
                {"name": "문의 응대", "task_type": "CS", "assignees": ["팀모노리스"]}
            )

        assert "날짜" not in mock_notion.pages.create.call_args.kwargs["properties"]

    @pytest.mark.asyncio
    async def test_slack_thread_is_attached_as_bookmark(self):
        """생성된 페이지에 슬랙 스레드가 북마크로 붙는다"""
        p1, p2 = self._patched()
        with p1, p2:
            tool = notion_tools.get_create_ops_task_tool("db-1", THREAD_URL)

        mock_notion = MagicMock()
        mock_notion.pages.create.return_value = {"id": "page-3", "url": "u"}

        with patch.object(notion_tools, "notion", mock_notion):
            await tool.ainvoke(
                {"name": "보고서 작성", "task_type": "문서", "assignees": ["학회"]}
            )

        mock_notion.blocks.children.append.assert_called_once_with(
            block_id="page-3",
            children=[{"type": "bookmark", "bookmark": {"url": THREAD_URL}}],
        )


class TestGetDataSourceId:
    """database_id → data_source_id 변환"""

    def test_resolves_and_caches(self):
        """첫 조회 후에는 캐시를 쓴다"""
        common._cache_data_source_id.clear()

        mock_notion = MagicMock()
        mock_notion.databases.retrieve.return_value = {
            "data_sources": [{"id": "ds-abc", "name": "운영 DB"}]
        }

        with patch.object(common, "notion", mock_notion):
            first = common.get_data_source_id("db-1")
            second = common.get_data_source_id("db-1")

        assert first == "ds-abc"
        assert second == "ds-abc"
        mock_notion.databases.retrieve.assert_called_once_with("db-1")
