"""운영팀 Slack List 작업만 제공하는 독립 MCP 서버입니다."""

import os
from typing import Literal, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context, MCPServer
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
작업 중 대화는 에이전트 안에 두고, 실제 작업이 끝났을 때만 결과와 작업 중 만든
공유 가능한 산출물 링크를 한 번 게시합니다. 시행착오·경험은 중요한 내용이 있을
때만 포함합니다. 전사 지식 검색은 이 서버가 제공하지 않습니다.
start-slack-list-task로 맥락을 읽은 뒤 실제 작업에 착수하기 전에는 Knowledge MCP의
query_knowledge로 관련 과거 기록을 조사해야 합니다. 결과·범위에 영향을 주는
모호함이 남으면 현재 작업 시작자에게 질문하고 답을 받은 뒤 진행합니다.
""".strip()


def _client_display_name(context: Context) -> str:
    """MCP 초기화 정보의 클라이언트 이름을 팀원이 알아볼 표기로 바꿉니다."""
    client_params = context.session.client_params
    if client_params is None:
        return "알 수 없음"

    client_info = client_params.client_info
    identity = f"{client_info.name} {client_info.title or ''}".casefold()
    if "claude" in identity:
        return "Claude Code"
    if "codex" in identity:
        return "Codex"
    return client_info.title or client_info.name


def build_mcp(
    slack_client: AsyncWebClient | None = None,
) -> MCPServer:
    """운영팀 Slack 작업 MCP 서버를 만듭니다."""
    # 두 MCP는 같은 wfa.codle.io 호스트와 OAuth 메타데이터를 공유한다.
    resource_url = os.environ["MCP_RESOURCE_URL"]
    mcp: MCPServer = MCPServer(
        "team-monolith-operations-task",
        instructions=INSTRUCTIONS,
        token_verifier=AdminRailsTokenVerifier(),
        auth=admin_auth_settings(resource_url),
    )
    slack = slack_client or AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])

    @mcp.tool(
        name="start-slack-list-task",
        description=(
            "운영팀 Slack List 작업 행 링크로 작업을 시작합니다. List 필드와 요청 "
            "맥락, 현재 상태, 기존 작업 결과를 읽고 작업 기록 스레드를 만들거나 "
            "재사용합니다. 새 스레드는 요청 맥락 메시지의 채널에 만들고 연결은 "
            "Slack List에만 저장합니다. 반환된 맥락을 읽은 뒤 실제 작업 전에는 "
            "Knowledge MCP의 query_knowledge로 관련 과거 기록을 조사하고, 중요한 "
            "모호함은 작업 시작자에게 확인해야 합니다. 작업 중 대화를 게시하지 "
            "않습니다."
        ),
    )
    async def start_slack_list_task(list_url: str, context: Context) -> str:
        token = cast(AdminToken, get_access_token())
        return await start_task_from_slack_list(
            slack,
            list_url,
            token.email,
            _client_display_name(context),
        )

    @mcp.tool(
        description=(
            "운영팀 작업이 완료됐거나 막힘·인계로 종료될 때 작업 기록 스레드에 "
            "요약 한 건을 게시합니다. outputs에는 작업 중 만든 공유 가능한 산출물 "
            "링크를 모두 넣고 각 항목에 https:// 링크를 포함해야 합니다. 만든 링크가 "
            "없을 때만 빈 배열을 사용합니다. learnings에는 최종 접근을 바꿨거나 같은 "
            "실수를 막아 줄 중요한 시행착오·경험이 있을 때만 최대 3개 넣고, 없으면 "
            "생략합니다. 매 응답이나 중간 진행에는 사용하지 않습니다."
        )
    )
    async def publish_slack_task_result(
        list_url: str,
        status: Literal["completed", "blocked", "handoff"],
        summary: str,
        outputs: list[str],
        learnings: list[str] | None = None,
        reusable_findings: list[str] | None = None,
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
        os.environ["MCP_RESOURCE_URL"],
        streamable_http_path="/mcp/operate",
    )
