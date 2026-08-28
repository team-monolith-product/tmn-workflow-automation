"""운영팀 Slack 작업 전용 MCP 서버 테스트입니다."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.testclient import TestClient

from app.slack_task_mcp import _client_display_name, build_mcp, build_mcp_app

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
    assert "publish_slack_task_result" in response.text
    assert "query_knowledge" not in response.text


@pytest.mark.asyncio
async def test_작업_도구만_등록하고_중간기록은_두지_않는다(mcp_env):
    tools = await build_mcp().list_tools()

    assert [tool.name for tool in tools] == [
        "start-slack-list-task",
        "publish_slack_task_result",
    ]
    assert "post_slack_task_checkpoint" not in {tool.name for tool in tools}
    start_tool = next(tool for tool in tools if tool.name == "start-slack-list-task")
    assert set(start_tool.input_schema["properties"]) == {"list_url"}
    publish_tool = next(
        tool for tool in tools if tool.name == "publish_slack_task_result"
    )
    assert "outputs" in publish_tool.input_schema["required"]
    assert "learnings" not in publish_tool.input_schema["required"]


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
