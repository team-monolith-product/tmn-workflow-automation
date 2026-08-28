"""운영팀 Slack List 작업만 제공하는 독립 MCP 서버입니다."""

import os
from typing import Literal, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from slack_sdk.web.async_client import AsyncWebClient
from starlette.applications import Starlette

from app.mcp_common import (
    AdminRailsTokenVerifier,
    AdminToken,
    admin_auth_settings,
    build_streamable_http_app,
)
from service.slack_task_thread import publish_task_result, start_task_from_slack_list

INSTRUCTIONS = """
운영팀의 Slack List 작업을 시작하거나 재개하고, 현재 상태와 요청 맥락,
이전 작업 기록을 읽어 공용 작업 스레드로 연결합니다.
새 작업 스레드는 요청 맥락 메시지의 채널에 만들며, 관계와 상태는 Slack List에만
저장합니다.
작업 중 대화는 에이전트 안에 두고, 실제 작업이 끝났을 때만 결과와 선별한
시행착오·경험을 한 번 게시합니다. 전사 지식 검색은 이 서버가 제공하지 않습니다.
""".strip()


def allowed_emails_from_env() -> frozenset[str]:
    """쉼표로 구분한 운영팀 이메일 allowlist를 읽습니다."""
    emails = frozenset(
        item.strip().casefold()
        for item in os.environ.get("SLACK_TASK_MCP_ALLOWED_EMAILS", "").split(",")
        if item.strip()
    )
    if not emails:
        raise RuntimeError("SLACK_TASK_MCP_ALLOWED_EMAILS가 비어 있습니다.")
    return emails


class OperationsTokenVerifier(AdminRailsTokenVerifier):
    """유효한 사내 계정 중 운영팀 allowlist에 속한 사용자만 허용합니다."""

    def __init__(self, allowed_emails: frozenset[str]):
        self.allowed_emails = frozenset(email.casefold() for email in allowed_emails)

    async def verify_token(self, token: str) -> AccessToken | None:
        verified = await super().verify_token(token)
        if not isinstance(verified, AdminToken):
            return None
        if verified.email.casefold() not in self.allowed_emails:
            return None
        return verified


def build_mcp(
    slack_client: AsyncWebClient | None = None,
    allowed_emails: frozenset[str] | None = None,
) -> MCPServer:
    """운영팀 Slack 작업 MCP 서버를 만듭니다."""
    resource_url = os.environ["SLACK_TASK_MCP_RESOURCE_URL"]
    mcp: MCPServer = MCPServer(
        "team-monolith-operations-task",
        instructions=INSTRUCTIONS,
        token_verifier=OperationsTokenVerifier(
            allowed_emails if allowed_emails is not None else allowed_emails_from_env()
        ),
        auth=admin_auth_settings(resource_url),
    )
    slack = slack_client or AsyncWebClient(token=os.environ["SLACK_TASK_MCP_BOT_TOKEN"])

    @mcp.tool(
        name="start-slack-list-task",
        description=(
            "운영팀 Slack List 작업 행 링크로 작업을 시작합니다. List 필드와 요청 "
            "맥락, 현재 상태, 기존 작업 결과를 읽고 작업 기록 스레드를 만들거나 "
            "재사용합니다. 새 스레드는 요청 맥락 메시지의 채널에 만들고 연결은 "
            "Slack List에만 저장합니다. 작업 중 대화를 게시하지 않습니다."
        ),
    )
    async def start_slack_list_task(list_url: str) -> str:
        token = cast(AdminToken, get_access_token())
        return await start_task_from_slack_list(slack, list_url, token.email)

    @mcp.tool(
        description=(
            "운영팀 작업이 완료됐거나 막힘·인계로 종료될 때 작업 기록 스레드에 "
            "요약 한 건을 게시합니다. learnings에는 최종 접근을 바꿨거나 같은 실수를 "
            "막아 줄 시행착오·경험이 있을 때만 최대 3개 넣습니다. 매 응답이나 중간 "
            "진행에는 사용하지 않습니다."
        )
    )
    async def publish_slack_task_result(
        list_url: str,
        status: Literal["completed", "blocked", "handoff"],
        summary: str,
        learnings: list[str] | None = None,
        reusable_findings: list[str] | None = None,
        outputs: list[str] | None = None,
        validation: list[str] | None = None,
        remaining: list[str] | None = None,
        mark_completed: bool = False,
    ) -> str:
        token = cast(AdminToken, get_access_token())
        return await publish_task_result(
            client=slack,
            list_url=list_url,
            actor=token.email,
            status=status,
            summary=summary,
            learnings=learnings,
            reusable_findings=reusable_findings,
            outputs=outputs,
            validation=validation,
            remaining=remaining,
            mark_completed=mark_completed,
        )

    return mcp


def build_mcp_app(mcp: MCPServer) -> Starlette:
    """공용 호스트의 운영팀 전용 경로에 mount할 MCP 앱을 만듭니다."""
    return build_streamable_http_app(
        mcp,
        os.environ["SLACK_TASK_MCP_RESOURCE_URL"],
        streamable_http_path="/mcp/operate",
    )
