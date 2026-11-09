"""팀모노리스 로봇 도구를 MCP로 여는 배선 테스트"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.tools import tool
from mcp.server.mcpserver.exceptions import ToolError
from slack_sdk.errors import SlackApiError

from app.general import DATA_GUIDE, ROBOT_INSTRUCTIONS, SLACK_DATA_GUIDE
from app.knowledge_mcp import build_mcp
from app.robot_mcp import _as_text, parse_slack_thread_url
from app.tools.athena_tools import SLACK_RESULT_GUIDE, get_execute_athena_query_tool
from app.tools.notion_page_tools import get_notion_page_tools
from app.tools.python_tools import (
    CHART_GUIDE,
    NO_UPLOAD_GUIDE,
    get_execute_python_tool,
)
from app.tools.slack_place import SlackPlace

THREAD_URL = "https://monolith-keb2010.slack.com/archives/C01ROBOT/p1700000000123456"
REQUESTER = SimpleNamespace(email="operator@team-mono.com")


@pytest.fixture
def mcp_env(monkeypatch):
    """build_mcp가 읽는 환경 변수를 채웁니다."""
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://wfa.codle.io")


def _slack_error(error: str) -> SlackApiError:
    return SlackApiError(error, {"ok": False, "error": error})


def _slack(
    members: list[str], *, bot_is_member: bool = True, kind: str = "channel"
) -> AsyncMock:
    client = AsyncMock()
    channel = {"id": "C01ROBOT", "is_im": kind == "im", "is_mpim": kind == "mpim"}
    # DM 과 그룹 DM 응답에는 is_member 가 없다.
    if kind == "channel":
        channel["is_member"] = bot_is_member
    client.conversations_info.return_value = {"channel": channel}
    client.users_lookupByEmail.side_effect = _slack_error("users_not_found")
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
    ), patch("app.robot_mcp.build_robot_tools", build_tools):
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
    ), patch("app.robot_mcp.build_robot_tools", AsyncMock(return_value=[draft_sms])):
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
    ), patch("app.robot_mcp.build_robot_tools", build_tools):
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


async def test_이메일_대소문자가_달라도_같은_사람으로_찾는다(mcp_env):
    build_tools = AsyncMock(return_value=[])

    with patch(
        "app.robot_mcp.get_access_token",
        return_value=SimpleNamespace(email="Operator@Team-Mono.com"),
    ), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch(
        "app.robot_mcp.build_robot_tools", build_tools
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


async def test_로봇이_볼_수_없는_채널은_채널에_없는_것과_같은_문구다(mcp_env):
    """문구가 다르면 비공개 채널이 있는지와 로봇이 있는지가 드러납니다."""
    slack = _slack(["U01OWNER"])
    slack.conversations_members.side_effect = _slack_error("channel_not_found")

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ):
        with pytest.raises(ToolError) as hidden:
            await build_mcp(slack).call_tool(
                "list_robot_tools", {"slack_thread_url": THREAD_URL}
            )
        with pytest.raises(ToolError) as outsider:
            await build_mcp(_slack(["U02OTHER"])).call_tool(
                "list_robot_tools", {"slack_thread_url": THREAD_URL}
            )

    assert str(hidden.value) == str(outsider.value)
    slack.conversations_info.assert_not_awaited()


def test_목록_결과는_JSON으로_돌려준다():
    assert _as_text([{"title": "가입자", "ok": True}]) == (
        '[{"title": "가입자", "ok": true}]'
    )


async def test_스레드가_없으면_슬랙_전송_지침을_빼고_차트가_안_간다고_알린다(mcp_env):
    tools = {t.name: t for t in await build_mcp(_slack([])).list_tools()}

    assert SLACK_RESULT_GUIDE not in tools["execute_athena_query"].description
    assert NO_UPLOAD_GUIDE in tools["execute_python"].description
    # 슬랙 로봇에는 지침이 그대로 붙는다.
    slack_athena = get_execute_athena_query_tool(
        SlackPlace(AsyncMock(), "C01ROBOT", "1.2", AsyncMock())
    )
    assert slack_athena.description.endswith(SLACK_RESULT_GUIDE)


async def test_list로_만든_도구를_바로_이은_call이_한_번_쓴다(mcp_env):
    @tool
    async def post_channel_message(text: str) -> str:
        """채널에 올립니다."""
        return "올렸습니다"

    build_tools = AsyncMock(return_value=[post_channel_message])
    call = {
        "slack_thread_url": THREAD_URL,
        "name": "post_channel_message",
        "arguments": {"text": "공지"},
    }

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch("app.robot_mcp.build_robot_tools", build_tools):
        mcp = build_mcp(_slack(["U01OWNER"]))
        listed = await mcp.call_tool(
            "list_robot_tools", {"slack_thread_url": THREAD_URL}
        )
        await mcp.call_tool("call_robot_tool", call)
        assert build_tools.await_count == 1
        # 한 번 꺼내 쓰면 지운다. 도구 구성이 바뀌었을 수 있으니 다음은 새로 만든다.
        await mcp.call_tool("call_robot_tool", call)
        assert build_tools.await_count == 2

    assert '"properties": {"text"' in listed.content[0].text


async def test_사용자_목록_캐시에_없으면_이메일로_다시_찾는다(mcp_env):
    """사용자 목록은 하루 캐시라 그 뒤에 들어온 사람이 없습니다."""
    slack = _slack(["U03NEW"])
    slack.users_lookupByEmail.side_effect = None
    slack.users_lookupByEmail.return_value = {"user": {"id": "U03NEW"}}
    build_tools = AsyncMock(return_value=[])

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async", AsyncMock(return_value={})
    ), patch("app.robot_mcp.build_robot_tools", build_tools):
        await build_mcp(slack).call_tool(
            "list_robot_tools", {"slack_thread_url": THREAD_URL}
        )

    slack.users_lookupByEmail.assert_awaited_once_with(email="operator@team-mono.com")
    assert build_tools.await_args.args[1] == "U03NEW"


@pytest.mark.parametrize("kind", ["im", "mpim"])
async def test_DM과_그룹_DM은_로봇이_참여자라_통과한다(mcp_env, kind):
    build_tools = AsyncMock(return_value=[])

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch("app.robot_mcp.build_robot_tools", build_tools):
        await build_mcp(_slack(["U01OWNER"], kind=kind)).call_tool(
            "list_robot_tools", {"slack_thread_url": THREAD_URL}
        )

    build_tools.assert_awaited_once()


@pytest.mark.parametrize(
    "url",
    [
        "https://monolith-keb2010.enterprise.slack.com/archives/C01ROBOT/p1700000000123456",
        "https://app.slack.com/client/T01TEAM/C01ROBOT/thread/C01ROBOT-1700000000.123456",
    ],
)
def test_엔터프라이즈_도메인과_브라우저_주소창_링크도_받는다(url):
    assert parse_slack_thread_url(url) == ("C01ROBOT", "1700000000.123456")


async def test_서버_안내에_슬랙_전송_지침이_없다(mcp_env):
    """스레드 없이 부르면 결과가 슬랙으로 가지 않으므로 그 지침이 있으면 안 됩니다."""
    instructions = build_mcp(_slack([])).instructions

    assert "Athena 데이터베이스 가이드" in instructions
    assert "show_result_to_user" not in instructions
    assert "upload_file" not in instructions
    assert "슬랙에 자동으로 업로드" not in instructions


def test_슬랙_로봇의_지침은_그대로다():
    assert ROBOT_INSTRUCTIONS == DATA_GUIDE + SLACK_DATA_GUIDE
    assert "show_result_to_user=True" in ROBOT_INSTRUCTIONS
    assert "upload_file" in ROBOT_INSTRUCTIONS


async def test_스레드가_없으면_파이썬_설명에_슬랙_차트_안내가_없다(mcp_env):
    tools = {t.name: t for t in await build_mcp(_slack([])).list_tools()}
    slack_python = get_execute_python_tool(
        SlackPlace(AsyncMock(), "C01ROBOT", "1.2", AsyncMock())
    )

    assert CHART_GUIDE not in tools["execute_python"].description
    assert (
        "슬랙"
        not in tools["execute_python"].description.split("**슬랙 스레드 없음**")[0]
    )
    assert CHART_GUIDE in slack_python.description


async def test_웹_도구는_열지_않는다(mcp_env):
    names = {t.name for t in await build_mcp(_slack([])).list_tools()}

    assert "tavily_search_results_json" not in names
    assert "get_web_page_from_url" not in names


async def test_스레드_도구_목록에는_바로_부를_수_있는_도구와_똑같은_것이_없다(mcp_env):
    """노션 페이지처럼 스레드와 상관없는 도구는 빼고, 스레드에 결과를 올리는
    Athena 처럼 모양이 다른 도구는 남깁니다."""

    @tool
    async def post_channel_message(text: str) -> str:
        """채널에 올립니다."""
        return text

    place = SlackPlace(AsyncMock(), "C01ROBOT", "1700000000.123456", AsyncMock())
    built = [
        post_channel_message,
        get_notion_page_tools()[0],
        get_execute_athena_query_tool(place),
    ]

    with patch("app.robot_mcp.get_access_token", return_value=REQUESTER), patch(
        "app.robot_mcp.get_email_to_user_id_async",
        AsyncMock(return_value={"operator@team-mono.com": "U01OWNER"}),
    ), patch("app.robot_mcp.build_robot_tools", AsyncMock(return_value=built)):
        result = await build_mcp(_slack(["U01OWNER"])).call_tool(
            "list_robot_tools", {"slack_thread_url": THREAD_URL}
        )

    listed = result.content[0].text
    assert "## post_channel_message" in listed
    assert "## execute_athena_query" in listed
    assert f"## {get_notion_page_tools()[0].name}" not in listed
