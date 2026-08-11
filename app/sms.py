"""
문자 발송 도구입니다. 슬랙 봇과 MCP 가 같은 함수를 부릅니다.

발송 규칙이 진입점마다 갈리면 한쪽이 중복 차단을 빠뜨렸을 때 실제 사람에게
문자가 두 번 갑니다. 검색이 run_query 하나를 공유하는 것과 같은 이유이고,
이쪽이 더 위험합니다.

requested_by 는 도구 인자가 아니라 클로저입니다. 에이전트가 남의 이름으로
발송 기록을 남기는 경로를 없앱니다.

슬랙에서는 발송 도구를 에이전트에게 주지 않습니다. draft_sms 로 승인 카드만
올리고, 실제 발송은 사람이 버튼이나 ✅ 를 눌렀을 때 app.sms_approval 이
부릅니다. "컨펌 받고 쓰라"는 docstring 은 모델에게 하는 부탁이지 게이트가
아닙니다. MCP 는 사람이 직접 명령을 치는 자리라 발송 도구를 그대로 둡니다.
"""

import asyncio
from typing import Any

from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.knowledge.users import fetch_user_emails
from service.sms import result as sms_result
from service.sms import send as sms_send


def send_blocking(
    campaign: str,
    template_name: str | None,
    content: str | None,
    targets: list[dict],
    subject: str | None,
    requested_by: str,
    entrypoint: str,
) -> dict[str, Any]:
    """시트 접근과 벤더 호출을 한 스레드에서 처리합니다.

    gspread 도 urllib 도 동기라 이벤트 루프에서 직접 부르면 봇 4개와
    스케줄러가 공유하는 루프가 시트·벤더 응답을 기다리는 동안 멈춥니다.

    Args:
        campaign: 발송 건 식별자
        template_name: 문안 파일 이름 (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        targets: 수신자 목록
        subject: LMS 제목
        requested_by: 시킨 사람 이메일
        entrypoint: slack · mcp

    Returns:
        dict[str, Any]: send_campaign 결과
    """
    return sms_send.send_campaign(
        campaign=campaign,
        rows=targets,
        template_name=template_name,
        content=content,
        requested_by=requested_by,
        entrypoint=entrypoint,
        subject=subject,
    )


def record_blocking(campaign: str, statuses: dict[str, str]) -> list[dict[str, Any]]:
    """도달 결과를 기록하고 재발송 대상을 돌려줍니다.

    Args:
        campaign: 발송 건 식별자
        statuses: 번호 -> 결과코드

    Returns:
        list[dict[str, Any]]: 실패한 수신자 목록
    """
    return sms_result.record(campaign, statuses)


def summary_blocking(campaign: str) -> dict[str, Any]:
    """캠페인 현황을 조회합니다.

    Args:
        campaign: 발송 건 식별자

    Returns:
        dict[str, Any]: campaign_summary 결과
    """
    return sms_send.campaign_summary(campaign)


def render_preview(result: dict[str, Any]) -> str:
    """미리보기 결과를 사람이 읽을 형태로 만듭니다.

    Args:
        result: send.preview 결과

    Returns:
        str: 요약 + 본문
    """
    return (
        f"{result['message_type']} · 치환 후 최대 {result['max_bytes']}byte "
        f"· 대상 {result['targets']}명\n"
        f"{'─' * 40}\n{result['sample']}\n{'─' * 40}"
    )


def render_sent(campaign: str, result: dict[str, Any]) -> str:
    """발송 결과를 사람이 읽을 형태로 만듭니다.

    Args:
        campaign: 발송 건 식별자
        result: send.send_campaign 결과

    Returns:
        str: 접수 요약
    """
    if result["sent"] == 0:
        return f"[{campaign}] 대상 {result['requested']}명이 모두 이미 발송된 상태라 보내지 않았습니다."
    return (
        f"[{campaign}] {result['message_type']} 접수 완료 — "
        f"발송 {result['sent']}명"
        + (f" · 중복 제외 {result['skipped']}명" if result["skipped"] else "")
        + f"\nmessageKey {result['message_key']}"
    )


def get_sms_tools(
    client: AsyncWebClient, user_id: str | None, channel: str, thread_ts: str
) -> list:
    """슬랙 봇용 문자 도구를 반환합니다.

    Args:
        client: 슬랙 클라이언트
        user_id: 요청자의 Slack 사용자 ID
        channel: 채널 ID
        thread_ts: 스레드 ts

    Returns:
        list: [미리보기, 초안·승인요청, 현황] 도구
    """
    from app import sms_approval

    @tool
    async def preview_sms(
        targets: list[dict],
        template: str | None = None,
        content: str | None = None,
    ) -> str:
        """
        문자를 보내지 않고 문안·메시지 타입·길이만 확인합니다.
        template 은 저장된 문안 파일 이름(discord 등), content 는 이번에 직접 쓴 본문이며
        둘 중 하나만 줍니다. targets 는
        [{"to": "010...", "name": "홍길동", "var1": "..."}] 형식입니다.
        이름 치환은 본문에 [*이름*], 그 외 값은 [*1*]~[*8*] 로 씁니다.
        """
        return render_preview(sms_send.preview(targets, template, content))

    @tool
    async def draft_sms(
        campaign: str,
        targets: list[dict],
        template: str | None = None,
        content: str | None = None,
        subject: str | None = None,
    ) -> str:
        """
        문자 발송 초안을 슬랙에 올려 사람의 승인을 요청합니다. 실제로 발송하지 않습니다.
        문자를 보내야 하면 항상 이 도구를 씁니다. 데이터를 조회해 명단을 만들었다면
        그 결과를 targets 로 넘기고, 본문은 content 에 직접 씁니다.
        campaign 은 이 발송 건의 식별자이며 같은 값으로 이미 보낸 번호는 자동으로 빠집니다.
        """
        if not targets:
            return "수신자가 없습니다. 명단을 먼저 확정하세요."
        try:
            summary = sms_send.preview(targets, template, content)
        except (ValueError, FileNotFoundError) as error:
            return f"문안 확인 실패: {error}"

        return await sms_approval.post_draft(
            client,
            channel=channel,
            thread_ts=thread_ts,
            draft={
                "campaign": campaign,
                "template_name": template,
                "content": content,
                "targets": targets,
                "subject": subject,
                "requested_by": await _actor(client, user_id),
            },
            summary=summary,
        )

    @tool
    async def sms_campaign_status(campaign: str) -> str:
        """
        발송 건의 진행 현황을 봅니다. 접수 성공·결과 미상·실패 건수를 셉니다.
        """
        row = await asyncio.to_thread(summary_blocking, campaign)
        if not row or not row.get("total"):
            return f"[{campaign}] 발송 기록이 없습니다."
        return (
            f"[{campaign}] 총 {row['total']} · 접수성공 {row['accepted']}"
            f" · 결과미상 {row['unknown']} · 실패 {row['failed']}"
        )

    return [preview_sms, draft_sms, sms_campaign_status]


async def _actor(client: AsyncWebClient, user_id: str | None) -> str:
    """발송 기록에 남길 요청자를 정합니다.

    Args:
        client: 슬랙 클라이언트
        user_id: Slack 사용자 ID

    Returns:
        str: 이메일. 못 찾으면 slack:<id>
    """
    emails = await fetch_user_emails(client)
    return emails.get(user_id, f"slack:{user_id}")
