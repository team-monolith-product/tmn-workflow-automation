"""한 FastAPI 프로세스에서 두 MCP 경로를 제공하는 배선을 검증합니다."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.knowledge_mcp import build_mcp as build_knowledge_mcp
from app.knowledge_mcp import build_mcp_app as build_knowledge_mcp_app
from app.mcp_dispatcher import MCPPathDispatcher
from app.slack_task_mcp import build_mcp as build_operations_mcp
from app.slack_task_mcp import build_mcp_app as build_operations_mcp_app

ADMIN = {
    "id": 7,
    "email": "operator@team-mono.com",
    "permissions": ["Role"],
    "tenants": ["test_class"],
}
RESOURCE_URL = "https://wfa.codle.io"
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_한_앱에서_두_MCP_경로와_공용_OAuth_메타데이터를_연다(monkeypatch):
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", RESOURCE_URL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    knowledge_mcp = build_knowledge_mcp()
    operations_mcp = build_operations_mcp()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with (
            knowledge_mcp.session_manager.run(),
            operations_mcp.session_manager.run(),
        ):
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount(
        "/",
        MCPPathDispatcher(
            build_knowledge_mcp_app(knowledge_mcp),
            build_operations_mcp_app(operations_mcp),
        ),
    )

    with TestClient(app, base_url=RESOURCE_URL) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/.well-known/oauth-protected-resource").status_code == 200
        assert client.get("/not-an-mcp-route").status_code == 404
        assert (
            client.post("/mcp", json=TOOLS_LIST, headers=MCP_HEADERS).status_code == 401
        )
        assert (
            client.post(
                "/mcp/operate", json=TOOLS_LIST, headers=MCP_HEADERS
            ).status_code
            == 401
        )

        with patch("app.mcp_common.get_me", AsyncMock(return_value=ADMIN)):
            knowledge_tools = client.post(
                "/mcp",
                json=TOOLS_LIST,
                headers=MCP_HEADERS | {"Authorization": "Bearer valid-token"},
            )
            operations_tools = client.post(
                "/mcp/operate",
                json=TOOLS_LIST,
                headers=MCP_HEADERS | {"Authorization": "Bearer valid-token"},
            )

    assert '"name":"query_knowledge"' in knowledge_tools.text
    assert '"name":"start-slack-list-task"' not in knowledge_tools.text
    assert '"name":"start-slack-list-task"' in operations_tools.text
    assert '"name":"publish_slack_task_result"' in operations_tools.text
    assert '"name":"query_knowledge"' not in operations_tools.text
