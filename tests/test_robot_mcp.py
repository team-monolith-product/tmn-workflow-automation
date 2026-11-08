"""팀모노리스 로봇 도구를 MCP로 여는 배선 테스트"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.tools import tool
from mcp.server.mcpserver.exceptions import ToolError
from slack_sdk.errors import SlackApiError

from app.general import _build_tools
from app.knowledge_mcp import build_mcp
from app.robot_mcp import _as_text, parse_slack_thread_url, placeless_tools
from app.tools.athena_tools import SLACK_RESULT_GUIDE, get_execute_athena_query_tool
from app.tools.python_tools import NO_UPLOAD_GUIDE

THREAD_URL = "https://monolith-keb2010.slack.com/archives/C01ROBOT/p1700000000123456"
REQUESTER = SimpleNamespace(email="operator@team-mono.com")


@pytest.fixture
def mcp_env(monkeypatch):
    """build_mcp가 읽는 환경 변수를 채웁니다."""
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://wfa.codle.io")


def _slack(members: list[str], *, bot_is_member: bool = True) -> AsyncMock:
    client = AsyncMock()
    client.conversations_info.return_value = {
        "channel": {"id": "C01ROBOT", "is_member": bot_is_member}
    }
    client.conversations_members.return_value = {
        "members": members,
        "response_metadata": {"next_cursor": ""},
    }
    return client


def test_스레드_링크에서_채널과_ts를_꺼낸다():
    assert parse_slack_thread_url(THREAD_URL) == ("C01ROBOT", "1700000000.123456")


def test_답글_링크는_부모_스레드를_가리킨다():
    url = (
        "https://monolith-keb2010.slack.com/archives/C01ROBOT/p1700000999000001"
        "?thread_ts=1700000000.123456&cid=C01ROBOT"
    )

    assert parse_slack_thread_url(url) == ("C01ROBOT", "1700000000.123456")


def test_슬랙_메시지_링크가_아니면_거절한다():
    with pytest.raises(ToolError, match="슬랙 메시지 링크가 아닙니다"):
        parse_slack_thread_url("https://www.notion.so/abc")


async def test_데이터_분석_도구가_스레드_없이_등록된다(mcp_env):
    tools = {t.name: t for t in await build_mcp(_slack([])).list_tools()}

    assert {
        "execute_athena_query",
        "list_redash_dashboards",
        "read_redash_dashboard",
        "read_redash_query",
        "execute_python",
        "get_notion_page",
        "list_robot_tools",
        "call_robot_tool",
    } <= set(tools)
    # 스레드가 없으면 슬랙 전송 스위치는 아무 일도 하지 않으므로 보이지 않는다.
    athena = tools["execute_athena_query"].input_schema
    assert set(athena["properties"]) == {"query", "database"}
    assert athena["required"] == ["query", "database"]


async def test_athena_도구는_로봇과_같은_실행_경로를_탄다(mcp_env):
    results = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "n"}]},
                {"Data": [{"VarCharValue": "3"}]},
            ]
        }
    }
    with patch(
        "app.tools.athena_tools.athena.execute_and_wait",
        AsyncMock(return_value=results),
    ) as execute:
        result = await build_mcp(_slack([])).call_tool(
            "execute_athena_query",
            {"query": "SELECT 3 AS n", "database": "jce_prd"},
        )

    execute.assert_awaited_once_with("SELECT 3 AS n", database="jce_prd")
    assert "3" in result.content[0].text


async def test_스레드_도구는_질문자와_그_스레드로_로봇_도구를_만든다(mcp_env):
    @tool
    async def post_channel_message(text: str) -> str:
        """채널에 올립니다."""
        return f"올렸습니다: {text}"

    slack = _slack(["U01OWNER"])
    build_tools = AsyncMock(return_value=[post_channel_message])

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch("app.robot_mcp._build_tools", build_tools):
        result = await build_mcp(slack).call_tool(
            "call_robot_tool",
            {
                "slack_thread_url": THREAD_URL,
                "name": "post_channel_message",
                "arguments": {"text": "공지"},
            },
        )

    assert result.content[0].text == "올렸습니다: 공지"
    client, user_id, channel, thread_ts, _say = build_tools.await_args.args
    assert (client, user_id, channel, thread_ts) == (
        slack,
        "U01OWNER",
        "C01ROBOT",
        "1700000000.123456",
    )


async def test_그_스레드에_없는_도구는_쓸_수_있는_도구를_알려준다(mcp_env):
    @tool
    async def draft_sms(content: str) -> str:
        """문자 초안을 올립니다."""
        return "올렸습니다"

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch("app.robot_mcp._build_tools", AsyncMock(return_value=[draft_sms])):
        with pytest.raises(ToolError, match="쓸 수 있는 도구: draft_sms"):
            await build_mcp(_slack(["U01OWNER"])).call_tool(
                "call_robot_tool",
                {
                    "slack_thread_url": THREAD_URL,
                    "name": "draft_ops_mail",
                    "arguments": {},
                },
            )


async def test_들어가_있지_않은_채널의_스레드는_받지_않는다(mcp_env):
    build_tools = AsyncMock()

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch("app.robot_mcp._build_tools", build_tools):
        with pytest.raises(ToolError, match="들어가 있지 않은 채널"):
            await build_mcp(_slack(["U02OTHER"])).call_tool(
                "list_robot_tools", {"slack_thread_url": THREAD_URL}
            )

    build_tools.assert_not_awaited()


async def test_슬랙에_없는_이메일은_거절한다(mcp_env):
    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async", AsyncMock(return_value={})
    ):
        with pytest.raises(ToolError, match="슬랙 사용자가 없습니다"):
            await build_mcp(_slack([])).call_tool(
                "list_robot_tools", {"slack_thread_url": THREAD_URL}
            )


async def test_스레드_없이_여는_도구는_로봇이_쓰는_도구와_같은_이름이다(mcp_env):
    """로봇 도구 이름이 바뀌면 MCP 쪽 이름이 혼자 남지 않게 막습니다."""
    with patch("app.general.find_channel_task_list", return_value=None), patch(
        "app.common.get_task_type_options", return_value=[]
    ), patch("app.common.get_component_options", return_value=[]), patch(
        "app.common.get_active_projects", return_value={}
    ), patch(
        "app.common.get_status_options", return_value=["완료"]
    ):
        robot = await _build_tools(
            AsyncMock(), None, "C01ROBOT", "1700000000.123456", AsyncMock()
        )

    assert {t.name for t in placeless_tools()} <= {t.name for t in robot}


async def test_이메일_대소문자가_달라도_같은_사람으로_찾는다(mcp_env):
    build_tools = AsyncMock(return_value=[])

    with patch(
        "app.robot_mcp.get_access_token",
        return_value=SimpleNamespace(email="Operator@Team-Mono.com"),
    ), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch(
        "app.robot_mcp._build_tools", build_tools
    ):
        await build_mcp(_slack(["U01OWNER"])).call_tool(
            "list_robot_tools", {"slack_thread_url": THREAD_URL}
        )

    assert build_tools.await_args.args[1] == "U01OWNER"


async def test_로봇이_없는_공개_채널은_초대하라고_알려준다(mcp_env):
    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ):
        with pytest.raises(ToolError, match="로봇을 채널에 초대해"):
            await build_mcp(_slack(["U01OWNER"], bot_is_member=False)).call_tool(
                "list_robot_tools", {"slack_thread_url": THREAD_URL}
            )


async def test_로봇이_볼_수_없는_채널은_초대하라고_알려준다(mcp_env):
    slack = _slack(["U01OWNER"])
    slack.conversations_info.side_effect = SlackApiError(
        "channel_not_found", {"ok": False, "error": "channel_not_found"}
    )

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ):
        with pytest.raises(ToolError, match="로봇이 볼 수 없는 채널"):
            await build_mcp(slack).call_tool(
                "list_robot_tools", {"slack_thread_url": THREAD_URL}
            )


def test_목록_결과는_JSON으로_돌려준다():
    assert _as_text([{"title": "가입자", "ok": True}]) == (
        '[{"title": "가입자", "ok": true}]'
    )


async def test_스레드가_없으면_슬랙_전송_지침을_빼고_차트가_안_간다고_알린다(mcp_env):
    tools = {t.name: t for t in await build_mcp(_slack([])).list_tools()}

    assert SLACK_RESULT_GUIDE not in tools["execute_athena_query"].description
    assert NO_UPLOAD_GUIDE in tools["execute_python"].description
    # 슬랙 로봇에는 지침이 그대로 붙는다.
    slack_athena = get_execute_athena_query_tool(say=AsyncMock(), thread_ts="1.2")
    assert slack_athena.description.endswith(SLACK_RESULT_GUIDE)
