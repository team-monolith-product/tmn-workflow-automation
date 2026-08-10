"""지식베이스 MCP 서버 테스트"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.knowledge_mcp import (
    AdminRailsTokenVerifier,
    AdminToken,
    build_mcp,
    build_mcp_app,
)

ADMIN = {
    "id": 7,
    "email": "lch@team-mono.com",
    "permissions": ["Role"],
    "tenants": ["test_class"],
}

RESOURCE_URL = "https://wfa.codle.io"
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def mcp_env(monkeypatch):
    """build_mcp와 build_mcp_app이 읽는 환경 변수를 채웁니다."""
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("KNOWLEDGE_MCP_RESOURCE_URL", RESOURCE_URL)


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


def test_리소스_메타데이터와_mcp_경로를_연다(mcp_env):
    paths = {route.path for route in build_mcp_app(build_mcp()).routes}

    assert "/mcp" in paths
    assert "/.well-known/oauth-protected-resource" in paths


def test_401이_가리키는_메타데이터_주소가_실제로_열려_있다(mcp_env):
    # SDK는 resource_metadata 주소를 리소스 URL 뒤에 이어붙여 만든다. 리소스
    # URL에 /mcp를 넣으면 여기가 어긋나 클라이언트가 인가 서버를 못 찾는다.
    mcp = build_mcp()
    with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
        response = client.post("/mcp", json=TOOLS_LIST, headers=MCP_HEADERS)

    assert response.status_code == 401
    advertised = response.headers["www-authenticate"].split('resource_metadata="')[1]
    advertised = advertised.rstrip('"')
    assert advertised == f"{RESOURCE_URL}/.well-known/oauth-protected-resource"


def test_운영_호스트로_온_요청은_통과하고_다른_호스트는_막힌다(mcp_env):
    # SDK는 host 인자 기본값을 로컬 서버로 읽어 DNS rebinding 보호를 켜고,
    # 그러면 운영 도메인으로 온 요청이 421로 막힌다. 인증 뒤에 있어 토큰을
    # 받은 클라이언트만 부딪히므로 401을 확인하는 것으로는 잡히지 않는다.
    mcp = build_mcp()
    headers = MCP_HEADERS | {"Authorization": "Bearer valid-token"}

    with patch("app.knowledge_mcp.get_me", AsyncMock(return_value=ADMIN)):
        with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
            allowed = client.post("/mcp", json=TOOLS_LIST, headers=headers)
            blocked = client.post(
                "/mcp", json=TOOLS_LIST, headers=headers | {"Host": "evil.example"}
            )

    assert allowed.status_code == 200
    assert "query_knowledge" in allowed.text
    assert blocked.status_code == 421


def test_FastAPI에_붙여도_기존_라우트가_먼저_잡힌다(mcp_env):
    # main.py 의 배선을 그대로 재현한다. "/"에 mount 하므로 기존 라우트를
    # 가리지 않는지, lifespan 이 세션 매니저를 띄우는지가 여기서 갈린다.
    mcp = build_mcp()
    mcp_app = build_mcp_app(mcp)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    api = FastAPI(lifespan=lifespan)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    api.mount("/", mcp_app)

    with TestClient(api, base_url=RESOURCE_URL) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/.well-known/oauth-protected-resource").status_code == 200

        response = client.post("/mcp", json=TOOLS_LIST, headers=MCP_HEADERS)
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_질의_도구가_등록된다(mcp_env):
    tools = await build_mcp().list_tools()

    assert [tool.name for tool in tools] == [
        "query_knowledge",
        "preview_sms",
        "send_sms",
    ]
