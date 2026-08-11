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

from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from app import sms_approval
from app.sms_render import render_preview
from service.knowledge.users import fetch_user_emails
from service.sms import ledger
from service.sms import roster
from service.sms import send as sms_send


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
        subject 는 장문(LMS)일 때 수신자에게 보이는 제목입니다. 생략하면 campaign 이
        그대로 제목이 되므로, 본문이 길면 사람이 읽을 제목을 반드시 주세요.
        """
        try:
            sheet_id = await asyncio.to_thread(roster.sheet_for, channel)
        except roster.NotConnected as error:
            return str(error)

        problems = sms_send.check(targets, template_name=template, content=content)
        if problems:
            # 하나씩 돌려주면 고치고 다시 부르고를 반복한다.
            return "보내기 전에 고칠 것:\n" + "\n".join(f"- {p}" for p in problems)

        summary = sms_send.preview(targets, template, content)

        return await sms_approval.post_draft(
            client,
            sms_approval.Draft(
                campaign=campaign,
                targets=targets,
                requested_by=await _actor(client, user_id),
                channel=channel,
                thread_ts=thread_ts,
                spreadsheet_id=sheet_id,
                template_name=template,
                content=content,
                subject=subject,
            ),
            summary,
        )

    @tool
    async def connect_participant_sheet(spreadsheet: str) -> str:
        """
        이 채널에 참가자 스프레드시트를 연결합니다.
        "이 채널에 <구글시트 주소> 연결해줘" 같은 요청에 사용합니다.
        연결한 뒤로 이 채널에서 보내는 문자의 발송이력이 그 시트의
        '발송이력' 탭에 쌓입니다. 주소를 그대로 붙여넣어도 됩니다.
        """
        try:
            sheet_id = ledger.parse_spreadsheet_id(spreadsheet)
        except ValueError as error:
            return str(error)
        actor = await _actor(client, user_id)
        await asyncio.to_thread(roster.connect_sheet, channel, sheet_id, actor)
        return (
            f"이 채널을 참가자 시트 `{sheet_id}` 에 연결했습니다.\n"
            "앞으로 이 채널에서 보내는 문자의 발송이력이 그 시트의 '발송이력' 탭에 쌓입니다."
        )

    @tool
    async def disconnect_participant_sheet() -> str:
        """
        이 채널의 참가자 스프레드시트 연결을 끊습니다.
        연결이 끊기면 이 채널에서는 문자를 보낼 수 없습니다. 이미 쌓인 이력은 그대로 남습니다.
        """
        removed = await asyncio.to_thread(roster.disconnect_sheet, channel)
        if removed is None:
            return "이 채널에는 연결된 참가자 시트가 없습니다."
        return "연결을 끊었습니다. 이미 쌓인 발송이력은 시트에 그대로 있습니다."

    @tool
    async def sms_campaign_status(campaign: str) -> str:
        """
        발송 건의 진행 현황을 봅니다. 접수 성공·결과 미상·실패 건수를 셉니다.
        """
        try:
            sheet_id = await asyncio.to_thread(roster.sheet_for, channel)
        except roster.NotConnected as error:
            return str(error)
        row = await asyncio.to_thread(sms_send.campaign_summary, sheet_id, campaign)
        if not row or not row.get("total"):
            return f"[{campaign}] 발송 기록이 없습니다."
        return (
            f"[{campaign}] 총 {row['total']} · 접수성공 {row['accepted']}"
            f" · 접수미상 {row['unknown']} · 중복제외 {row['duplicate']}"
            f" · 접수실패 {row['failed']}"
            "\n도달 여부가 아니라 벤더 접수 기준입니다."
        )

    return [
        preview_sms,
        draft_sms,
        connect_participant_sheet,
        disconnect_participant_sheet,
        sms_campaign_status,
    ]


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
