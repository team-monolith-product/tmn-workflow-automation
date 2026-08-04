"""지식베이스 MCP 서버 테스트"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.knowledge_mcp import AdminRailsTokenVerifier, AdminToken, build_mcp

ADMIN = {
    "id": 7,
    "email": "lch@team-mono.com",
    "permissions": ["Role"],
    "tenants": ["test_class"],
}


@pytest.mark.asyncio
async def test_유효한_토큰은_이메일을_실어_돌려준다():
    with patch("app.knowledge_mcp.get_me", AsyncMock(return_value=ADMIN)):
        token = await AdminRailsTokenVerifier().verify_token("valid-token")

    assert isinstance(token, AdminToken)
    assert token.email == "lch@team-mono.com"
    assert token.token == "valid-token"
    assert token.client_id == "7"


@pytest.mark.asyncio
async def test_유효하지_않은_토큰은_None이다():
    with patch("app.knowledge_mcp.get_me", AsyncMock(return_value=None)):
        token = await AdminRailsTokenVerifier().verify_token("expired-token")

    assert token is None


def test_리소스_메타데이터와_mcp_경로를_연다(monkeypatch):
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("KNOWLEDGE_MCP_RESOURCE_URL", "https://wfa.codle.io")

    paths = {route.path for route in build_mcp().streamable_http_app().routes}

    assert "/mcp" in paths
    assert "/.well-known/oauth-protected-resource" in paths


def test_401이_가리키는_메타데이터_주소가_실제로_열려_있다(monkeypatch):
    # SDK는 resource_metadata 주소를 리소스 URL 뒤에 이어붙여 만든다. 리소스
    # URL에 /mcp를 넣으면 여기가 어긋나 클라이언트가 인가 서버를 못 찾는다.
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("KNOWLEDGE_MCP_RESOURCE_URL", "https://wfa.codle.io")

    mcp = build_mcp()
    with TestClient(mcp.streamable_http_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401
    advertised = response.headers["www-authenticate"].split('resource_metadata="')[1]
    advertised = advertised.rstrip('"')
    assert advertised == "https://wfa.codle.io/.well-known/oauth-protected-resource"


def test_FastAPI에_붙여도_기존_라우트가_먼저_잡힌다(monkeypatch):
    # main.py 의 배선을 그대로 재현한다. "/"에 mount 하므로 기존 라우트를
    # 가리지 않는지, lifespan 이 세션 매니저를 띄우는지가 여기서 갈린다.
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("KNOWLEDGE_MCP_RESOURCE_URL", "https://wfa.codle.io")

    mcp = build_mcp()
    mcp_app = mcp.streamable_http_app(stateless_http=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    api = FastAPI(lifespan=lifespan)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    api.mount("/", mcp_app)

    with TestClient(api) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/.well-known/oauth-protected-resource").status_code == 200

        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_검색_도구가_등록된다(monkeypatch):
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("KNOWLEDGE_MCP_RESOURCE_URL", "https://wfa.codle.io")

    tools = await build_mcp().list_tools()

    assert [tool.name for tool in tools] == ["search_knowledge"]
