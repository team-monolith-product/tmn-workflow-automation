"""
문자 발송 도구입니다. 슬랙 봇과 MCP 가 같은 함수를 부릅니다.

발송 규칙이 진입점마다 갈리면 한쪽이 중복 차단을 빠뜨렸을 때 실제 사람에게
문자가 두 번 갑니다. 검색이 search_items 하나를 공유하는 것과 같은 이유이고,
이쪽이 더 위험합니다.

requested_by 는 도구 인자가 아니라 클로저입니다. 에이전트가 남의 이름으로
발송 기록을 남기는 경로를 없앱니다.
"""

import asyncio
from typing import Any

from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.knowledge.users import fetch_user_emails
from service.sms import send as sms_send


def send_blocking(
    campaign: str,
    template_name: str,
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
        template_name: 문안 파일 이름
        targets: 수신자 목록
        subject: LMS 제목
        requested_by: 시킨 사람 이메일
        entrypoint: slack · mcp

    Returns:
        dict[str, Any]: send_campaign 결과
    """
    return sms_send.send_campaign(
        campaign=campaign,
        template_name=template_name,
        rows=targets,
        requested_by=requested_by,
        entrypoint=entrypoint,
        subject=subject,
    )


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
        "\n접수 성공이지 도달 확인이 아닙니다. 도달 결과는 뿌리오 웹에서 확인해야 합니다."
    )


def get_sms_tools(client: AsyncWebClient, user_id: str | None) -> list:
    """문자 발송 도구를 반환합니다.

    Args:
        client: 슬랙 클라이언트
        user_id: 요청자의 슬랙 사용자 ID

    Returns:
        list: [미리보기, 발송, 현황] 도구
    """

    async def _actor() -> str:
        emails = await fetch_user_emails(client)
        return emails.get(user_id, f"slack:{user_id}")

    @tool
    async def preview_sms(template: str, targets: list[dict]) -> str:
        """
        문자를 보내지 않고 문안·메시지 타입·길이만 확인합니다.
        발송 전에는 항상 이걸 먼저 실행해 사람에게 보여줍니다.
        template 은 문안 파일 이름(discord 등), targets 는
        [{"to": "010...", "name": "홍길동", "var1": "...", "var2": "..."}] 형식입니다.
        """
        return render_preview(sms_send.preview(template, targets))

    @tool
    async def send_sms(
        campaign: str, template: str, targets: list[dict], subject: str | None = None
    ) -> str:
        """
        문자를 실제로 발송합니다. 대외 발신이므로 반드시 사람의 컨펌을 받은 뒤에만 씁니다.
        campaign 은 이 발송 건의 식별자이고, 같은 campaign 으로 이미 보낸 번호는
        자동으로 빠집니다. 재발송이 필요하면 campaign 을 다르게 지정합니다.
        """
        actor = await _actor()
        result = await asyncio.to_thread(
            send_blocking, campaign, template, targets, subject, actor, "slack"
        )
        return render_sent(campaign, result)

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
            f" · 결과미상 {row['unknown']} · 중복제외 {row['duplicate']}"
            f" · 실패 {row['failed']}"
        )

    return [preview_sms, send_sms, sms_campaign_status]
