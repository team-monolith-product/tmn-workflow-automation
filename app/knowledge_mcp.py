"""
지식베이스를 MCP로 제공하는 서버입니다.

각자의 CLI 에이전트가 붙는 경로입니다. 슬랙 봇과 같은 run_query를 부르므로
어느 쪽에서 물어도 같은 것이 잡히고, 쓰기가 막히는 지점도 한 곳입니다.

인증은 admin-rails Doorkeeper가 발급한 토큰을 그대로 받습니다. 사내 OAuth
제공자가 거기 하나이고, 그 뒤가 Google SSO라 계정 관리가 이미 되어 있습니다.
토큰을 자체 검증하지 않고 admin-rails에 물어보는 이유는 Doorkeeper 토큰이
불투명 문자열이라 서명으로 확인할 것이 없어서입니다. 만료와 폐기 판정은
발급자만 할 수 있습니다.

FastAPI에 mount할 때 lifespan을 함께 넘겨야 합니다. 세션 매니저가 뜨지 않으면
/mcp 요청이 전부 실패합니다.
"""

import asyncio
import os
from typing import Any, cast
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from api.admin_rails import get_me
from service.knowledge.query import (
    DEFAULT_CHAR_LIMIT,
    QUERY_TOOL_DESCRIPTION,
    run_query,
)

INSTRUCTIONS = """
팀모노리스 사내 슬랙 공개 채널의 과거 대화가 쌓인 지식베이스입니다.
읽기 전용 SQL로 질의합니다.
"예전에 이거 어떻게 했었지", "이 에러 본 적 있나" 같은 질문에 씁니다.
""".strip()


class AdminToken(AccessToken):
    """어드민 이메일을 함께 나르는 토큰.

    query_log.actor에 실제 사람을 남기려면 도구 실행 시점에 이메일이 있어야
    하는데, AccessToken에는 담을 자리가 없어 넓힙니다.
    """

    email: str


class AdminRailsTokenVerifier(TokenVerifier):
    """admin-rails에 물어 토큰을 검증합니다."""

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Args:
            token: Authorization 헤더로 온 Bearer 토큰

        Returns:
            AccessToken | None: 검증에 성공하면 AdminToken. 실패하면 None
        """
        admin = await get_me(token)
        if admin is None:
            return None

        return AdminToken(
            token=token,
            client_id=str(admin["id"]),
            scopes=["public"],
            email=admin["email"],
        )


def build_mcp() -> MCPServer:
    """지식베이스 MCP 서버를 만듭니다.

    KNOWLEDGE_MCP_RESOURCE_URL에는 /mcp를 붙이지 않습니다. SDK가 401의
    resource_metadata 주소를 "이 값 + /.well-known/oauth-protected-resource"로
    만드는데, /mcp를 붙이면 실제로 열려 있는 경로와 어긋나 클라이언트가
    인가 서버를 찾지 못합니다.

    Returns:
        MCPServer: 도구가 등록된 서버
    """
    mcp: MCPServer = MCPServer(
        "team-monolith-knowledge",
        instructions=INSTRUCTIONS,
        token_verifier=AdminRailsTokenVerifier(),
        auth=AuthSettings(
            issuer_url=cast(Any, os.environ["ADMIN_RAILS_BASE_URL"]),
            resource_server_url=cast(Any, os.environ["KNOWLEDGE_MCP_RESOURCE_URL"]),
        ),
    )

    @mcp.tool(description=QUERY_TOOL_DESCRIPTION)
    async def query_knowledge(sql: str, char_limit: int = DEFAULT_CHAR_LIMIT) -> str:
        token = cast(AdminToken, get_access_token())
        # psycopg는 동기라 스레드에서 실행합니다.
        return await asyncio.to_thread(run_query, sql, token.email, "mcp", char_limit)

    return mcp


def build_mcp_app(mcp: MCPServer) -> Starlette:
    """MCP를 FastAPI에 mount할 ASGI 앱으로 만듭니다.

    transport_security를 넘기지 않으면 SDK가 host 인자 기본값 "127.0.0.1"을 보고
    로컬 서버로 판단해 DNS rebinding 보호를 켭니다. 그러면 허용 목록이
    루프백 주소뿐이라 운영 도메인으로 온 요청이 421 Invalid Host header로
    막힙니다. 인증을 통과한 요청만 여기까지 오므로 401 뒤에 가려집니다.

    세션은 들고 있지 않습니다(stateless_http). 세션이 있으면 파드가 늘어날 때
    sticky routing이 필요해지는데, 검색은 요청 하나로 끝나 이어갈 상태가 없습니다.

    Args:
        mcp: build_mcp가 만든 서버

    Returns:
        Starlette: /mcp와 리소스 메타데이터 경로를 여는 앱
    """
    host = urlparse(os.environ["KNOWLEDGE_MCP_RESOURCE_URL"]).netloc

    return mcp.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host],
            allowed_origins=[f"https://{host}"],
        ),
    )
