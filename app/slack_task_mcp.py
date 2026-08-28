"""운영팀 Slack List 작업만 제공하는 독립 MCP 서버입니다."""

import json
import os
from collections.abc import Mapping
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
from service.operational_postmortem import publish_operational_postmortem
from service.slack_task_thread import (
    publish_task_result,
    start_task_from_slack_list,
)

INSTRUCTIONS = """
운영팀 Slack List 작업의 요청 맥락과 작업 기록을 읽고 같은 스레드에서 이어서
작업하게 합니다. 작업 상태는 List 체크박스의 대기·완료 두 가지뿐입니다.
선행 조건이나 사용자 결정이 남아 있으면 List를 대기로 둡니다. 결과는 현재 에이전트
대화에서 사용자에게 먼저 리뷰받고, 사용자가 명시적으로 완료를 승인한 뒤에만 종료
결과를 게시하고 List를 완료 처리합니다.

작업 전과 중요한 변경 뒤에는 Knowledge MCP로 과거 기록을 충분히 조사합니다.
참고 자료는 중간 댓글로 게시하지 않고 누적했다가 최종 결과의 마지막 상세 영역에
한 번만 남깁니다. 최종 본문은 결과와 공유 산출물만 짧게 씁니다.

완료 직전에는 tmn-operating의 operational-postmortem 스킬로 작업 과정의 실패를
검토합니다. 실패가 있으면 확인된 사실과 가설을 분리해 원인을 조사하고, 실패를
유발한 결정이 내려진 요청 맥락 또는 작업 기록 스레드에 짧은 포스트모템을 남깁니다.
실제로 조사하거나 변경할 개선 작업이 있을 때만 @자동화를 표시하고 같은 List에 대기
작업을 만듭니다. 개선 작업의 요청 맥락과 작업 기록은 포스트모템 스레드로 연결되어,
완료 결과가 원래 포스트모템 스레드로 돌아옵니다. 전사 지식 검색 자체는 이 서버가
제공하지 않습니다.
""".strip()


def _client_display_name(context: Context) -> str:
    """MCP 초기화 정보의 클라이언트 이름을 팀원이 알아볼 표기로 바꿉니다."""
    client_params = context.session.client_params
    identity = ""
    fallback = "MCP 클라이언트"
    if client_params is not None:
        client_info = client_params.client_info
        identity = f"{client_info.name} {client_info.title or ''}".casefold()
        fallback = client_info.title or client_info.name
    headers = context.headers
    if isinstance(headers, Mapping):
        identity = f"{identity} {headers.get('user-agent', '')}".casefold()
    if "claude" in identity:
        return "Claude Code"
    if "codex" in identity:
        return "Codex"
    return fallback


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
            "Knowledge MCP의 query_knowledge를 반복 사용해 같은 사업·동일 업무, "
            "유사 사업·업무 패턴, 관련 요구사항·결정·실패 사례를 폭넓게 조사합니다. "
            "표현과 범위를 바꿔도 관련 기록이 없으면 작업 시작자에게 기존 레퍼런스의 "
            "존재와 위치를 확인해야 합니다. 중요한 모호함도 작업 시작자에게 확인합니다. "
            "작업 중 요구사항이나 방향이 바뀔 때마다 변경분을 기준으로 같은 조사를 다시 "
            "수행하고 관련 출처에서 새 정보가 더 나오지 않을 때까지 탐색합니다. 실제 "
            "판단에 사용한 참고 자료는 작업 중 누적하고 완료 결과의 마지막에 한 번만 "
            "남깁니다. 작업 중 대화를 게시하지 않습니다."
        ),
    )
    async def start_slack_list_task(list_url: str) -> str:
        token = cast(AdminToken, get_access_token())
        return await start_task_from_slack_list(
            slack,
            list_url,
            token.email,
        )

    @mcp.tool(
        description=(
            "이전 TMN Operating 플러그인과의 호환용입니다. 참고 자료를 Slack에 게시하지 "
            "않고 최종 결과의 references에 누적하라고 반환합니다. 새 플러그인은 이 도구를 "
            "사용하지 않습니다."
        )
    )
    async def record_slack_task_references(
        list_url: str,
        reason: str,
        references: list[str],
    ) -> str:
        return json.dumps(
            {
                "posted": False,
                "recording": "final_only",
                "list_url": list_url,
                "reason": reason,
                "reference_count": len(references),
            },
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "사용자가 현재 에이전트 대화에서 결과를 리뷰하고 명시적으로 완료를 승인한 뒤에만 "
            "호출합니다. 작업 기록 스레드에 결과와 공유 산출물만 짧게 게시하고 List를 완료 "
            "처리합니다. 실제 승인 뒤 user_approved_completion=true를 전달해야 합니다. "
            "outputs는 '산출물 이름: https://공유링크' 형식으로 작성합니다. "
            "references에는 작업에서 실제로 사용한 참고 자료 전체를 같은 형식으로 중복 없이 "
            "넣으며, 메시지 마지막 상세 영역에 한 번만 표시됩니다. 서버는 "
            "실행 도구와 작업 시작부터 종료까지의 전체 시간을 자동 기록합니다. model, "
            "reasoning_effort, 토큰 항목, conversation_turns에는 런타임에서 직접 확인한 값만 "
            "넣고, 알 수 없으면 생략합니다. 생략된 값은 '수집되지 않음'으로 표시합니다. "
            "선행 조건, 사용자 결정, 외부 처리가 남아 있으면 호출하지 않고 대기로 둡니다."
        )
    )
    async def publish_slack_task_result(
        list_url: str,
        user_approved_completion: Literal[True],
        summary: str,
        outputs: list[str],
        context: Context,
        references: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_output_tokens: int | None = None,
        total_tokens: int | None = None,
        conversation_turns: int | None = None,
    ) -> str:
        return await publish_task_result(
            client=slack,
            list_url=list_url,
            summary=summary,
            outputs=outputs,
            references=references,
            client_name=_client_display_name(context),
            model=model,
            reasoning_effort=reasoning_effort,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            total_tokens=total_tokens,
            conversation_turns=conversation_turns,
        )

    @mcp.tool(
        name="publish-operational-postmortem",
        description=(
            "operational-postmortem 스킬이 사실·가설·인과관계·놓친 신호를 조사한 뒤에만 "
            "호출합니다. 포스트모템은 실패가 발견된 곳이 아니라 실패를 유발한 결정이 "
            "내려진 target_thread_url에 게시합니다. 이 URL은 현재 작업의 요청 맥락 또는 "
            "작업 기록 스레드여야 합니다. 양쪽에 원인이 있으면 주된 원인의 스레드에 원문을 "
            "남기고 related_thread_url에 링크만 남깁니다. 실제 조사·변경 작업이 있을 때만 "
            "improvement_task_title, improvement_target, completion_criteria를 전달합니다. 이때 "
            "@자동화를 표시하고 같은 Slack List에 대기 작업을 만들며, 해당 작업 결과가 원래 "
            "포스트모템 스레드로 돌아오도록 연결합니다. references는 항상 마지막에 표시됩니다."
        ),
    )
    async def publish_operational_postmortem_result(
        list_url: str,
        incident_key: str,
        target_thread_url: str,
        title: str,
        expected: str,
        actual: str,
        confirmed_causes: list[str] | None = None,
        hypotheses: list[str] | None = None,
        missed_signals: list[str] | None = None,
        investigation_items: list[str] | None = None,
        system_changes: list[str] | None = None,
        improvement_task_title: str | None = None,
        improvement_target: str | None = None,
        completion_criteria: list[str] | None = None,
        references: list[str] | None = None,
        related_thread_url: str | None = None,
    ) -> str:
        token = cast(AdminToken, get_access_token())
        return await publish_operational_postmortem(
            client=slack,
            actor=token.email,
            list_url=list_url,
            incident_key=incident_key,
            target_thread_url=target_thread_url,
            title=title,
            expected=expected,
            actual=actual,
            confirmed_causes=confirmed_causes,
            hypotheses=hypotheses,
            missed_signals=missed_signals,
            investigation_items=investigation_items,
            system_changes=system_changes,
            improvement_task_title=improvement_task_title,
            improvement_target=improvement_target,
            completion_criteria=completion_criteria,
            references=references,
            related_thread_url=related_thread_url,
        )

    return mcp


def build_mcp_app(mcp: MCPServer) -> Starlette:
    """공용 호스트의 운영팀 전용 경로에 mount할 MCP 앱을 만듭니다."""
    return build_streamable_http_app(
        mcp,
        os.environ["MCP_RESOURCE_URL"],
        streamable_http_path="/mcp/operate",
    )
