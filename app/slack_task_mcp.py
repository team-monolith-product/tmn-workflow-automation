"""운영팀 Slack List 작업만 제공하는 독립 MCP 서버입니다."""

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
from service.slack_task_thread import (
    publish_task_result,
    record_task_references,
    start_task_from_slack_list,
)

INSTRUCTIONS = """
운영팀의 Slack List 작업을 시작하거나 재개하고, 현재 상태와 요청 맥락,
이전 작업 기록을 읽어 공용 작업 스레드로 연결합니다.
새 작업 스레드는 요청 맥락 메시지의 채널에 만들며, 관계와 상태는 Slack List에만
저장합니다.
작업 중 일반 대화는 에이전트 안에 둡니다. 다만 초기 조사와 진행 중 재조사 게이트를
통과할 때마다 실제 판단에 사용한 자료를 record_slack_task_references로 작업 스레드에
한 번 남기고, 실제 작업이 끝났을 때 결과를 한 번 게시합니다.
결과는 중요한 현재 상태와 다음 행동, 이름이 붙은 공유 산출물 링크를 중심으로
최소한으로 작성합니다. 비슷한 사업에서도 계획·실행·검토 방식을 바꿀 중요한 결정과
이유, 요구사항, 실수 방지 규칙도 남기며, 이런 정보는 산출물에 있어도 반복할 수
있습니다. 작업 과정, 당연한 선택, 일반적인 검증, List에 이미 보이는 상태는
반복하지 않습니다. 전사 지식 검색은 이 서버가 제공하지 않습니다.
완료는 요청한 산출물을 전달하고 검증을 마쳤으며, 사용자의 승인·결정이나 외부
처리 대기가 남지 않았을 때만 선언합니다. 단순 응답 완료나 사용자 입력 대기는
완료가 아닙니다. completed 결과는 기본으로 Slack List도 완료 처리합니다.
종료 결과 반환값의 outcome이 partial_success이면 결과 댓글은 이미 게시된 상태입니다.
반환된 permalink를 사용하고 새 댓글을 게시하지 않습니다. 같은 도구를 다시
호출하더라도 저장된 메시지를 수정할 뿐 새 댓글을 만들지 않습니다.
start-slack-list-task로 맥락을 읽은 뒤 실제 작업에 착수하기 전에는 Knowledge MCP의
query_knowledge를 반복 사용해 같은 사업·동일 업무, 유사 사업·업무 패턴, 관련
요구사항·결정·실패 사례를 각각 조사해야 합니다. 정확한 이름에서 결과가 없으면
핵심어·유사 표현·산출물 유형으로 넓히고, 새 검색이 같은 자료만 반복할 때까지
탐색합니다. 그래도 관련 기록을 찾지 못하면 작업 시작자에게 기존 레퍼런스나
관련 채널·문서가 있는지 묻고 답을 받은 뒤 진행합니다. 결과·범위에 영향을 주는
모호함도 현재 작업 시작자에게 질문합니다.
작업 중 추가 요구사항, 범위·방향·대상·산출물·일정의 변경, 새로운 고유 명사나
제약, 기존 판단과 충돌하는 정보가 나오면 변경분을 반영하기 전에 같은 조사 절차를
다시 수행합니다. 이전 검색 결과를 기준으로 변경된 개념과 연결된 직접 사례,
유사 패턴, 요구사항·결정·실패 사례를 추가로 추적하고, 서로 다른 관련 출처에서
새로운 사실이나 판단이 더 나오지 않을 때까지 탐색합니다.
각 조사 게이트에는 검색 결과 전체가 아니라 실제 판단·계획·산출물에 사용한 문서와
Knowledge 원문 링크를 이름과 함께 남깁니다. 종료 결과에도 사용한 참고 자료 전체를
중복 없이 다시 전달합니다. 시작 댓글에는 실행 도구를 표시하지 않습니다. 종료할 때
도구, 모델, Effort, 토큰, 전체 시간, 대화 턴과 참고 자료를 하나의 접힌 상세 영역에
모읍니다. 모델·Effort·토큰·대화 턴은 런타임에서 확인된 값만 전달하고 설정값이나
추측값으로 채우지 않습니다.
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
            "수행하고 관련 출처에서 새 정보가 더 나오지 않을 때까지 탐색합니다. 각 조사 "
            "게이트가 끝나면 실제 판단에 사용한 자료를 record_slack_task_references로 "
            "같은 작업 스레드에 남깁니다. "
            "작업 중 대화를 게시하지 "
            "않습니다."
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
            "초기 조사 또는 요구사항·방향 변경에 따른 재조사를 마친 직후, 실제 판단에 "
            "사용한 문서와 Knowledge 원문을 작업 스레드에 기록합니다. reason에는 조사 "
            "계기 또는 변경점을 짧게 쓰고, references의 각 항목은 반드시 "
            "'자료 이름: https://공유링크' 형식으로 작성합니다. 검색 결과 전체나 사용하지 "
            "않은 자료, 원문 로그는 넣지 않습니다. 같은 게이트에는 한 번만 호출합니다."
        )
    )
    async def record_slack_task_references(
        list_url: str,
        reason: str,
        references: list[str],
    ) -> str:
        return await record_task_references(
            client=slack,
            list_url=list_url,
            reason=reason,
            references=references,
        )

    @mcp.tool(
        description=(
            "운영팀 작업이 완료됐거나 막힘·인계로 종료될 때 작업 기록 스레드에 "
            "최소 요약 한 건을 게시합니다. summary에는 '무엇을 만들었다'는 작업 일지보다 "
            "결과의 의미와 필요한 다음 행동을 씁니다. 예: 'PDF를 제작했습니다'보다 "
            "'컨소사에 공유가 필요합니다'. outputs의 각 항목은 반드시 "
            "'산출물 이름: https://공유링크' 형식으로 작성합니다. 시행착오·검증·남은 일은 "
            "다음 사람의 판단이나 행동을 실제로 바꿀 때만 포함합니다. 비슷한 사업에서도 "
            "계획·실행·검토 방식을 바꿀 중요한 결정과 이유, 요구사항, 주요 실수와 방지책은 "
            "reusable_findings 또는 learnings에 남깁니다. 이런 고가치 정보는 산출물에 있어도 "
            "반복할 수 있습니다. 당연한 선택, 통상적인 품질 확인, List에 이미 보이는 상태는 "
            "반복하지 않습니다. references에는 각 조사 게이트에서 실제로 사용한 참고 자료 "
            "전체를 '자료 이름: https://공유링크' 형식으로 중복 없이 넣습니다. 서버는 "
            "실행 도구와 작업 시작부터 종료까지의 전체 시간을 자동 기록합니다. model, "
            "reasoning_effort, 토큰 항목, conversation_turns에는 런타임에서 직접 확인한 값만 "
            "넣고, 알 수 없으면 생략합니다. 생략된 값은 '수집되지 않음'으로 표시합니다. "
            "실행 메타 정보와 참고 자료는 최종 메시지에서 하나의 접힐 수 있는 상세 영역으로 "
            "표시됩니다. "
            "completed는 요청 산출물과 검증이 끝나고 사용자 승인·결정이나 외부 처리 대기가 "
            "없을 때만 사용하며, 기본으로 List도 완료 처리합니다. List 갱신만 실패해 "
            "outcome이 partial_success이면 permalink의 결과가 이미 게시된 것이므로 별도 "
            "결과 댓글을 다시 게시하지 않습니다."
        )
    )
    async def publish_slack_task_result(
        list_url: str,
        status: Literal["completed", "blocked", "handoff"],
        summary: str,
        outputs: list[str],
        context: Context,
        learnings: list[str] | None = None,
        reusable_findings: list[str] | None = None,
        validation: list[str] | None = None,
        remaining: list[str] | None = None,
        references: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_output_tokens: int | None = None,
        total_tokens: int | None = None,
        conversation_turns: int | None = None,
        mark_completed: bool = True,
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
