"""
문자 발송 승인·추적 핸들러

흐름:
1. draft_sms 도구가 명단·초안을 버튼과 함께 슬랙에 올린다 (여기서는 발송하지 않는다)
2. 사람이 [발송] 버튼을 누르거나 초안 메시지에 ✅ 이모지를 달면 승인된다
   (대화로 "보내줘"라고 하면 LLM이 send_drafted_sms 도구로 같은 경로를 탄다)
3. 뿌리오로 발송 → Playwright 로 웹 발송결과 확인 → 실패분만 재발송
4. 최종 결과를 스레드에 보고한다

대외 발신이므로 승인 없이 발송되는 경로는 만들지 않는다.
"""

import asyncio

from cachetools import TTLCache
from slack_sdk.web.async_client import AsyncWebClient

from app.event_dedup import is_duplicate_event
from service import sms

APPROVE_REACTIONS = {
    "white_check_mark",
    "heavy_check_mark",
    "ballot_box_with_check",
    "o",
    "ok",
}

# ponytail: 승인 대기 초안은 프로세스 메모리에만 둔다. 봇 재시작 시 초안은 사라지고
# 다시 요청해야 한다 — 발송 자체는 승인 시점에 끝나므로 유실돼도 문자는 새지 않는다.
_DRAFTS: TTLCache = TTLCache(maxsize=100, ttl=86400)
_MESSAGE_TS_TO_DRAFT_ID: TTLCache = TTLCache(maxsize=100, ttl=86400)

# create_task 참조를 유지하여 GC 방지
_background_tasks: set[asyncio.Task] = set()


def register_draft(draft_id: str, message_ts: str, draft: dict) -> None:
    """승인 대기 초안을 등록합니다.

    Args:
        draft_id: 초안 식별자 (버튼 value)
        message_ts: 초안 카드 메시지 ts (✅ 이모지 승인용)
        draft: {"channel", "thread_ts", "recipients", "template", "subject"}
    """
    _DRAFTS[draft_id] = draft
    _MESSAGE_TS_TO_DRAFT_ID[message_ts] = draft_id


async def _replace_card(client: AsyncWebClient, body: dict, text: str) -> None:
    """초안 카드를 결과 문구로 바꿔 버튼을 없앱니다."""
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


async def approve_draft(
    draft_id: str, approver: str | None, client: AsyncWebClient
) -> str:
    """초안을 승인하고 발송·추적을 백그라운드로 시작합니다.

    Args:
        draft_id: 초안 식별자
        approver: 승인한 슬랙 사용자 ID (없으면 라벨만 생략)
        client: 슬랙 클라이언트

    Returns:
        str: 처리 결과 메시지
    """
    draft = _DRAFTS.pop(draft_id, None)
    if draft is None:
        return "이미 처리되었거나 만료된 초안입니다."

    task = asyncio.create_task(_send_and_report(draft_id, draft, approver, client))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return f"{len(draft['recipients'])}명에게 발송을 시작합니다."


async def _send_and_report(
    draft_id: str, draft: dict, approver: str | None, client: AsyncWebClient
) -> None:
    """발송하고 결과가 모두 확정될 때까지 추적한 뒤 스레드에 보고합니다."""

    async def on_progress(text: str) -> None:
        await client.chat_postMessage(
            channel=draft["channel"], thread_ts=draft["thread_ts"], text=text
        )

    approver_label = f"<@{approver}>" if approver else "요청자"
    await on_progress(
        f"{approver_label} 승인 — 문자 발송을 시작합니다. (대상 {len(draft['recipients'])}명)"
    )
    try:
        report = await sms.send_and_confirm(
            draft["recipients"],
            draft["template"],
            draft["subject"],
            on_progress,
            ref_prefix=draft_id,
        )
    except Exception as error:
        # 백그라운드 태스크라 예외가 슬랙에 안 보이면 발송이 멈춘 줄도 모른다.
        await on_progress(f":x: 발송 중 오류가 발생했습니다: `{error}`")
        raise

    await client.chat_postMessage(
        channel=draft["channel"],
        thread_ts=draft["thread_ts"],
        text=sms.format_report(report),
    )


def register_sms_handlers(app) -> None:
    """문자 발송 승인 관련 슬랙 핸들러를 등록합니다.

    Args:
        app: AsyncApp 인스턴스
    """

    @app.action("sms_send")
    async def handle_sms_send(ack, body, client):
        """[발송] 버튼 클릭"""
        await ack()
        approver = body["user"]["id"]
        message = await approve_draft(body["actions"][0]["value"], approver, client)
        await _replace_card(
            client, body, f":white_check_mark: <@{approver}> 승인 — {message}"
        )

    @app.action("sms_cancel")
    async def handle_sms_cancel(ack, body, client):
        """[취소] 버튼 클릭"""
        await ack()
        _DRAFTS.pop(body["actions"][0]["value"], None)
        await _replace_card(
            client,
            body,
            f":x: <@{body['user']['id']}> 취소 — 발송하지 않았습니다.",
        )

    @app.event("reaction_added")
    async def handle_reaction_added(body, client):
        """초안 카드에 ✅ 이모지를 달면 승인으로 본다."""
        if is_duplicate_event(body):
            return

        event = body["event"]
        if event["reaction"] not in APPROVE_REACTIONS:
            return

        draft_id = _MESSAGE_TS_TO_DRAFT_ID.get(event["item"]["ts"])
        if draft_id is None:
            return

        await approve_draft(draft_id, event["user"], client)
