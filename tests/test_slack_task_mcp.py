"""운영팀 Slack 작업 전용 MCP 서버 테스트입니다."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.testclient import TestClient

from app.slack_task_mcp import (
    INSTRUCTIONS,
    _client_display_name,
    build_mcp,
    build_mcp_app,
)
from service.slack_task_list import ChannelTaskList

ADMIN = {
    "id": 7,
    "email": "operator@team-mono.com",
    "permissions": ["Role"],
    "tenants": ["test_class"],
}
RESOURCE_URL = "https://wfa.codle.io"
OPERATIONS_MCP_PATH = "/mcp/operate"
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", RESOURCE_URL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


def test_공용_호스트의_운영팀_경로에_mcp와_메타데이터를_연다(mcp_env):
    mcp = build_mcp()
    paths = {route.path for route in build_mcp_app(mcp).routes}

    assert OPERATIONS_MCP_PATH in paths
    assert "/.well-known/oauth-protected-resource" in paths


def test_운영팀_경로의_401은_공용_OAuth_메타데이터를_가리킨다(mcp_env):
    mcp = build_mcp()
    with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
        response = client.post(
            OPERATIONS_MCP_PATH, json=TOOLS_LIST, headers=MCP_HEADERS
        )

    assert response.status_code == 401
    advertised = response.headers["www-authenticate"].split('resource_metadata="')[1]
    advertised = advertised.rstrip('"')
    assert advertised == f"{RESOURCE_URL}/.well-known/oauth-protected-resource"


def test_admin_rails가_인증한_사내_계정에_작업_도구를_노출한다(mcp_env):
    mcp = build_mcp()
    headers = MCP_HEADERS | {"Authorization": "Bearer valid-token"}
    internal_user = ADMIN | {"email": "outside@team-mono.com"}

    with patch("app.mcp_common.get_me", AsyncMock(return_value=internal_user)):
        with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
            response = client.post(
                OPERATIONS_MCP_PATH, json=TOOLS_LIST, headers=headers
            )

    assert response.status_code == 200
    assert "start-slack-list-task" in response.text
    assert "create_slack_list_task" in response.text
    assert "publish_slack_task_result" in response.text
    assert '"name":"query_knowledge"' not in response.text


@pytest.mark.asyncio
async def test_작업과_조사근거_도구만_등록한다(mcp_env):
    tools = await build_mcp().list_tools()

    assert [tool.name for tool in tools] == [
        "start-slack-list-task",
        "create_slack_list_task",
        "record_slack_task_references",
        "publish_slack_task_result",
    ]
    assert "post_slack_task_checkpoint" not in {tool.name for tool in tools}
    start_tool = next(tool for tool in tools if tool.name == "start-slack-list-task")
    assert set(start_tool.input_schema["properties"]) == {"list_url"}
    create_tool = next(tool for tool in tools if tool.name == "create_slack_list_task")
    assert set(create_tool.input_schema["properties"]) == {"title", "due_date"}
    assert create_tool.input_schema["required"] == ["title"]
    reference_tool = next(
        tool for tool in tools if tool.name == "record_slack_task_references"
    )
    assert set(reference_tool.input_schema["properties"]) == {
        "list_url",
        "reason",
        "references",
    }
    publish_tool = next(
        tool for tool in tools if tool.name == "publish_slack_task_result"
    )
    assert "outputs" in publish_tool.input_schema["required"]
    assert "learnings" not in publish_tool.input_schema["required"]
    assert "references" in publish_tool.input_schema["properties"]
    assert {
        "model",
        "reasoning_effort",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "conversation_turns",
    } <= set(publish_tool.input_schema["properties"])
    assert "context" not in publish_tool.input_schema["properties"]


async def test_사용자_동의_후_운영_list에_작업_행을_만든다(mcp_env):
    client = AsyncMock()
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
    client.slackLists_items_create.return_value = {"item": {"id": "Rec01"}}
    task_list = ChannelTaskList(
        list_id="F01LIST",
        list_url="https://example.slack.com/lists/T1/F01LIST",
        name_column_id="ColTitle",
        completed_column_id="ColDone",
        assignee_column_id="ColOwner",
        due_date_column_id="ColDue",
        thread_column_id="ColSource",
    )

    with patch(
        "app.slack_task_mcp.get_access_token",
        return_value=SimpleNamespace(email="operator@team-mono.com"),
    ), patch("app.slack_task_mcp.find_channel_task_list", return_value=task_list):
        with patch(
            "app.slack_task_mcp.start_task_from_slack_list",
            AsyncMock(return_value='{"work_thread_created": true}'),
        ) as start:
            result = await build_mcp(client).call_tool(
                "create_slack_list_task",
                {"title": "계정 생성", "due_date": "2026-09-02"},
            )

    assert result.content[0].text == '{"work_thread_created": true}'
    fields = client.slackLists_items_create.await_args.kwargs["initial_fields"]
    assert {"column_id": "ColOwner", "user": ["U01OWNER"]} in fields
    assert {"column_id": "ColDue", "date": ["2026-09-02"]} in fields
    start.assert_awaited_once_with(
        client,
        f"{task_list.list_url}?record_id=Rec01",
        "operator@team-mono.com",
        default_channel_id="C077CABKVSQ",
    )


def test_링크가_없으면_생성_여부를_먼저_묻도록_지시한다():
    assert "행을\n새로 만들지 먼저 물어보고 답을 기다립니다" in INSTRUCTIONS
    assert "명시적으로 동의한 경우에만" in INSTRUCTIONS


@pytest.mark.parametrize(
    ("name", "title", "expected"),
    [
        ("codex-mcp-client", None, "Codex"),
        ("claude-code", None, "Claude Code"),
        ("codex-mcp-client", "AI coding agent", "Codex"),
        ("custom-client", "사내 에이전트", "사내 에이전트"),
    ],
)
def test_MCP_클라이언트_이름을_사람이_읽을_수_있게_표시한다(name, title, expected):
    client_info = SimpleNamespace(name=name, title=title)
    context = Mock()
    context.session.client_params.client_info = client_info

    assert _client_display_name(context) == expected


def test_MCP_초기화_정보가_없으면_일반_클라이언트로_표시한다():
    context = Mock()
    context.session.client_params = None

    assert _client_display_name(context) == "MCP 클라이언트"


def test_MCP_초기화_정보가_없어도_user_agent에서_codex를_찾는다():
    context = Mock()
    context.session.client_params = None
    context.headers = {"user-agent": "codex-mcp-client/1.0"}

    assert _client_display_name(context) == "Codex"
