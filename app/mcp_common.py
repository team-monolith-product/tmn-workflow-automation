"""사내 MCP 서버가 공유하는 admin-rails 인증과 HTTP 전송 설정입니다."""

import os
from typing import Any, cast
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from api.admin_rails import get_me


class AdminToken(AccessToken):
    """도구 실행 기록에 사용할 어드민 이메일을 함께 나르는 토큰입니다."""

    email: str


class AdminRailsTokenVerifier(TokenVerifier):
    """admin-rails에 물어 불투명 OAuth 토큰의 만료·폐기 상태를 검증합니다."""

    async def verify_token(self, token: str) -> AccessToken | None:
        admin = await get_me(token)
        if admin is None:
            return None

        return AdminToken(
            token=token,
            client_id=str(admin["id"]),
            scopes=["public"],
            email=admin["email"],
        )


def admin_auth_settings(resource_url: str) -> AuthSettings:
    """사내 admin-rails OAuth 제공자와 MCP 리소스 주소를 연결합니다."""
    return AuthSettings(
        issuer_url=cast(Any, os.environ["ADMIN_RAILS_BASE_URL"]),
        resource_server_url=cast(Any, resource_url),
    )


def build_streamable_http_app(
    mcp: MCPServer,
    resource_url: str,
    *,
    streamable_http_path: str = "/mcp",
) -> Starlette:
    """하나의 공개 호스트에서 MCP와 OAuth 메타데이터를 제공하는 앱을 만듭니다."""
    parsed = urlparse(resource_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP resource URL에는 http(s) scheme과 호스트가 필요합니다.")

    return mcp.streamable_http_app(
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[parsed.netloc],
            allowed_origins=[f"{parsed.scheme}://{parsed.netloc}"],
        ),
    )
