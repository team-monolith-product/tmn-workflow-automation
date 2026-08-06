"""
문자 발송 관련 LangChain Tools (뿌리오)

문자는 대외 발신이므로 도구가 바로 보내지 않는다. draft_sms 는 명단·초안을 슬랙에
버튼과 함께 올려 승인만 요청하고, 실제 발송은 app.sms_approval 이 버튼·이모지·
send_drafted_sms 승인을 받아 처리한다.
"""

import uuid
from typing import Annotated

from langchain_core.tools import tool

from app import sms_approval
from service import sms

ROSTER_PREVIEW_LIMIT = 20
CONTENT_PREVIEW_LIMIT = 2500


def _build_draft_blocks(
    draft_id: str, recipients: list[dict], preview: str, subject: str
) -> list[dict]:
    """승인 요청 카드 블록을 만듭니다.

    Args:
        draft_id: 초안 식별자
        recipients: 정규화된 수신자 목록
        preview: 첫 수신자 기준으로 렌더링한 본문 미리보기
        subject: LMS 제목

    Returns:
        list[dict]: 슬랙 blocks
    """
    roster = "\n".join(
        f"{index}. {recipient['name']} {recipient['phone']}"
        for index, recipient in enumerate(recipients[:ROSTER_PREVIEW_LIMIT], 1)
    )
    if len(recipients) > ROSTER_PREVIEW_LIMIT:
        roster += f"\n… 외 {len(recipients) - ROSTER_PREVIEW_LIMIT}명"

    message_type = sms.message_type(preview)
    subject_line = f" · 제목 `{subject}`" if message_type != "SMS" else ""

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "문자 발송 승인 요청"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*수신자 {len(recipients)}명*\n{roster}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*문안 미리보기*\n```{preview[:CONTENT_PREVIEW_LIMIT]}```",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"{message_type} · EUC-KR {sms.euckr_bytes(preview)}byte{subject_line}"
                        f" · 초안 ID `{draft_id}`\n"
                        "발송하려면 아래 버튼을 누르거나 이 메시지에 :white_check_mark: 를 달아주세요."
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "발송"},
                    "style": "primary",
                    "action_id": "sms_send",
                    "value": draft_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "문자를 발송할까요?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"수신자 {len(recipients)}명에게 즉시 발송됩니다.",
                        },
                        "confirm": {"type": "plain_text", "text": "발송"},
                        "deny": {"type": "plain_text", "text": "취소"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "취소"},
                    "action_id": "sms_cancel",
                    "value": draft_id,
                },
            ],
        },
    ]


def get_sms_tools(channel: str, thread_ts: str, user: str | None, client) -> list:
    """문자 발송 도구들을 슬랙 컨텍스트에 묶어 생성합니다.

    Args:
        channel: 채널 ID
        thread_ts: 스레드 ts
        user: 요청한 슬랙 사용자 ID (대화로 승인할 때 승인자로 기록)
        client: 슬랙 클라이언트

    Returns:
        list: [draft_sms, send_drafted_sms]
    """

    @tool
    async def draft_sms(
        recipients: Annotated[
            list[dict],
            '수신자 목록. [{"name": "홍길동", "phone": "010-1234-5678"}] 형태',
        ],
        content: Annotated[
            str,
            "문자 본문. {name} 을 쓰면 수신자 이름으로 치환된다. "
            "EUC-KR 90바이트를 넘으면 자동으로 LMS로 발송된다.",
        ],
        subject: Annotated[str, "LMS 제목 (90바이트 초과 시에만 사용됨)"] = "",
    ) -> str:
        """문자 발송 초안을 슬랙에 올려 사람의 승인을 요청합니다. 실제로 발송하지 않습니다.

        사용자가 문자 발송을 요청하면 이 도구를 먼저 호출해 명단과 문안을 확인받으세요.

        Args:
            recipients: 수신자 목록 (이름·전화번호)
            content: 문자 본문 템플릿
            subject: LMS 제목

        Returns:
            str: 초안 등록 결과와 다음 안내
        """
        if not recipients:
            return "수신자가 없습니다. 명단을 먼저 확정하세요."

        try:
            normalized = [
                {
                    "name": recipient.get("name", ""),
                    "phone": sms.normalize_phone(recipient["phone"]),
                }
                for recipient in recipients
            ]
        except ValueError as error:
            return f"수신번호 오류: {error}. 명단을 고쳐 다시 시도하세요."

        draft_id = uuid.uuid4().hex[:8]
        preview = sms.render_content(content, normalized[0]["name"])
        response = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"문자 발송 승인 요청 (수신자 {len(normalized)}명)",
            blocks=_build_draft_blocks(draft_id, normalized, preview, subject),
        )
        sms_approval.register_draft(
            draft_id,
            response["ts"],
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "recipients": normalized,
                "template": content,
                "subject": subject or "안내",
            },
        )

        return (
            f"초안 ID `{draft_id}` 로 승인 요청 카드를 슬랙에 올렸습니다. 아직 발송하지 않았습니다.\n"
            f"수신자 {len(normalized)}명 · {sms.message_type(preview)} "
            f"{sms.euckr_bytes(preview)}byte.\n"
            "답변에는 명단 요약과 문안을 간단히 정리하고, [발송] 버튼 또는 ✅ 이모지로 "
            "승인해 달라고 안내하세요. 승인 전에는 절대 send_drafted_sms 를 호출하지 마세요."
        )

    @tool
    async def send_drafted_sms(
        draft_id: Annotated[str, "draft_sms 가 반환한 초안 ID"],
    ) -> str:
        """승인된 문자 초안을 실제로 발송합니다.

        사용자가 이 대화에서 명시적으로 발송을 지시했을 때만 호출하세요
        (예: "보내줘", "발송해", "그대로 보내"). 초안을 만든 직후에 스스로 호출하면 안 됩니다.

        Args:
            draft_id: 발송할 초안 ID

        Returns:
            str: 발송 시작 여부. 진행 상황과 최종 결과는 봇이 스레드에 따로 보고합니다.
        """
        return await sms_approval.approve_draft(draft_id, user, client)

    return [draft_sms, send_drafted_sms]
