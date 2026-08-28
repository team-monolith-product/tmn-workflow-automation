"""운영팀 Slack 작업 전용 MCP 서버 테스트입니다."""

import importlib
import sys
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from app.mcp_common import AdminToken
from app.slack_task_mcp import OperationsTokenVerifier, build_mcp, build_mcp_app

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
    monkeypatch.setenv("SLACK_TASK_MCP_RESOURCE_URL", RESOURCE_URL)
    monkeypatch.setenv("SLACK_TASK_MCP_ALLOWED_EMAILS", ADMIN["email"])
    monkeypatch.setenv("SLACK_TASK_MCP_BOT_TOKEN", "xoxb-test")


@pytest.mark.asyncio
async def test_운영팀_계정만_토큰_검증을_통과한다():
    verifier = OperationsTokenVerifier(frozenset({ADMIN["email"].upper()}))

    with patch("app.mcp_common.get_me", AsyncMock(return_value=ADMIN)):
        allowed = await verifier.verify_token("valid-token")

    assert isinstance(allowed, AdminToken)
    assert allowed.email == ADMIN["email"]

    outside = ADMIN | {"email": "outside@team-mono.com"}
    with patch("app.mcp_common.get_me", AsyncMock(return_value=outside)):
        denied = await verifier.verify_token("valid-token")

    assert denied is None


def test_allowlist가_비면_서버가_시작되지_않는다(mcp_env, monkeypatch):
    monkeypatch.setenv("SLACK_TASK_MCP_ALLOWED_EMAILS", "")

    with pytest.raises(RuntimeError, match="비어 있습니다"):
        build_mcp()


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


def test_허용된_운영팀_계정에만_작업_도구를_노출한다(mcp_env):
    mcp = build_mcp()
    headers = MCP_HEADERS | {"Authorization": "Bearer valid-token"}

    with patch("app.mcp_common.get_me", AsyncMock(return_value=ADMIN)):
        with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
            allowed = client.post(OPERATIONS_MCP_PATH, json=TOOLS_LIST, headers=headers)

    assert allowed.status_code == 200
    assert "start-slack-list-task" in allowed.text
    assert "publish_slack_task_result" in allowed.text
    assert "query_knowledge" not in allowed.text

    outside = ADMIN | {"email": "outside@team-mono.com"}
    with patch("app.mcp_common.get_me", AsyncMock(return_value=outside)):
        with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
            denied = client.post(OPERATIONS_MCP_PATH, json=TOOLS_LIST, headers=headers)

    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_작업_도구만_등록하고_중간기록은_두지_않는다(mcp_env):
    tools = await build_mcp().list_tools()

    assert [tool.name for tool in tools] == [
        "start-slack-list-task",
        "publish_slack_task_result",
    ]
    assert "post_slack_task_checkpoint" not in {tool.name for tool in tools}


def test_독립_배포_진입점이_health를_제공한다(mcp_env):
    sys.modules.pop("operations_task_main", None)
    module = importlib.import_module("operations_task_main")

    try:
        with TestClient(module.app, base_url=RESOURCE_URL) as client:
            response = client.get("/health")
    finally:
        sys.modules.pop("operations_task_main", None)

    assert response.json() == {"status": "ok"}
