"""
문자 발송 승인·추적 핸들러입니다.

    draft_sms → 승인 카드 → [발송] 버튼 또는 ✅ → 발송 → 도달 확인 → 실패분 재발송 → 보고

대외 발신이라 사람이 누르기 전에는 아무것도 나가지 않습니다. 에이전트가 부를 수
있는 도구에는 발송 경로가 없습니다.

재발송은 campaign 을 -r2, -r3 으로 바꿔 부릅니다. UNIQUE (campaign, phone) 이
같은 이름으로는 두 번 못 보내게 막기 때문이고, 그래서 성공한 사람은 재발송
라운드에서 자동으로 빠집니다.
"""

import asyncio
from typing import Any

from cachetools import TTLCache
from slack_sdk.web.async_client import AsyncWebClient

from app.event_dedup import is_duplicate_event
from app.sms import record_blocking, render_sent, send_blocking
from service.sms import result as sms_result
from service.sms.templates import normalize_phone

APPROVE_REACTIONS = {
    "white_check_mark",
    "heavy_check_mark",
    "ballot_box_with_check",
    "o",
    "ok",
}

SEND_ROUNDS = 3  # 최초 발송 + 재발송 2회
POLL_INTERVAL_SECONDS = 60
POLL_LIMIT = 5  # 라운드당 최대 5분까지 도달 결과를 기다린다
ROSTER_PREVIEW_LIMIT = 20

# ponytail: 승인 대기 초안은 프로세스 메모리에만 둡니다. 봇이 재시작하면 초안이
# 사라져 다시 요청해야 하지만, 발송 자체는 DB 에 기록되므로 문자가 새지는 않습니다.
_DRAFTS: TTLCache = TTLCache(maxsize=100, ttl=86400)
_MESSAGE_TS_TO_DRAFT_ID: TTLCache = TTLCache(maxsize=100, ttl=86400)

# create_task 참조를 유지하여 GC 방지
_background_tasks: set[asyncio.Task] = set()


def _build_blocks(draft_id: str, draft: dict, summary: dict) -> list[dict]:
    """승인 카드 블록을 만듭니다.

    Args:
        draft_id: 초안 식별자
        draft: 발송 초안
        summary: send.preview 결과

    Returns:
        list[dict]: 슬랙 blocks
    """
    targets = draft["targets"]
    roster = "\n".join(
        f"{index}. {target.get('name', '')} {target['to']}"
        for index, target in enumerate(targets[:ROSTER_PREVIEW_LIMIT], 1)
    )
    if len(targets) > ROSTER_PREVIEW_LIMIT:
        roster += f"\n… 외 {len(targets) - ROSTER_PREVIEW_LIMIT}명"

    source = (
        f"문안 파일 `{draft['template_name']}`"
        if draft["template_name"]
        else "즉석 문안"
    )

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "문자 발송 승인 요청"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*`{draft['campaign']}` · 수신자 {len(targets)}명*\n{roster}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*문안 미리보기*\n```{summary['sample'][:2500]}```",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"{source} · {summary['message_type']} · 치환 후 최대 "
                        f"{summary['max_bytes']}byte · 초안 `{draft_id}`\n"
                        "발송하려면 버튼을 누르거나 이 메시지에 :white_check_mark: 를 달아주세요."
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
                            "text": f"수신자 {len(targets)}명에게 즉시 발송됩니다.",
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


async def post_draft(
    client: AsyncWebClient, *, channel: str, thread_ts: str, draft: dict, summary: dict
) -> str:
    """승인 카드를 올리고 초안을 등록합니다.

    Args:
        client: 슬랙 클라이언트
        channel: 채널 ID
        thread_ts: 스레드 ts
        draft: campaign·template_name·content·targets·subject·requested_by
        summary: send.preview 결과

    Returns:
        str: 에이전트에게 돌려줄 안내
    """
    draft_id = f"{draft['campaign']}-{len(_DRAFTS)}"
    response = await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"문자 발송 승인 요청 ({draft['campaign']} · {len(draft['targets'])}명)",
        blocks=_build_blocks(draft_id, draft, summary),
    )
    _DRAFTS[draft_id] = {**draft, "channel": channel, "thread_ts": thread_ts}
    _MESSAGE_TS_TO_DRAFT_ID[response["ts"]] = draft_id

    return (
        f"승인 카드를 올렸습니다. 아직 발송하지 않았습니다.\n"
        f"`{draft['campaign']}` · 수신자 {len(draft['targets'])}명 · "
        f"{summary['message_type']} 최대 {summary['max_bytes']}byte\n"
        "답변에는 명단과 문안을 정리해 보여주고, [발송] 버튼이나 ✅ 로 승인해 달라고 안내하세요."
    )


async def approve_draft(
    draft_id: str, approver: str | None, client: AsyncWebClient
) -> str:
    """초안을 승인하고 발송·추적을 백그라운드로 시작합니다.

    Args:
        draft_id: 초안 식별자
        approver: 승인한 슬랙 사용자 ID
        client: 슬랙 클라이언트

    Returns:
        str: 처리 결과 메시지
    """
    draft = _DRAFTS.pop(draft_id, None)
    if draft is None:
        return "이미 처리되었거나 만료된 초안입니다."

    task = asyncio.create_task(_send_and_report(draft, approver, client))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return f"{len(draft['targets'])}명에게 발송을 시작합니다."


async def _poll(phones: list[str], on_progress) -> dict[str, str]:
    """모든 번호의 도달 결과가 확정될 때까지 웹 발송결과를 폴링합니다.

    Args:
        phones: 조회할 번호 목록
        on_progress: 진행 보고 콜백

    Returns:
        dict[str, str]: 확정된 {번호: 결과코드}
    """
    statuses: dict[str, str] = {}
    for attempt in range(1, POLL_LIMIT + 1):
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        statuses = await sms_result.fetch_results(phones)
        await on_progress(
            f"도달 확인 {attempt}/{POLL_LIMIT} — 확정 {len(statuses)}/{len(phones)}건"
        )
        if len(statuses) == len(phones):
            break
    return statuses


async def _send_and_report(
    draft: dict, approver: str | None, client: AsyncWebClient
) -> None:
    """발송하고 도달 결과가 확정될 때까지 추적한 뒤 스레드에 보고합니다."""

    async def on_progress(text: str) -> None:
        await client.chat_postMessage(
            channel=draft["channel"], thread_ts=draft["thread_ts"], text=text
        )

    approver_label = f"<@{approver}>" if approver else "요청자"
    await on_progress(
        f"{approver_label} 승인 — 발송을 시작합니다. (대상 {len(draft['targets'])}명)"
    )

    targets = draft["targets"]
    all_phones = [normalize_phone(target["to"]) for target in targets]
    resolved: dict[str, str] = {}

    try:
        for round_no in range(1, SEND_ROUNDS + 1):
            campaign = (
                draft["campaign"]
                if round_no == 1
                else f"{draft['campaign']}-r{round_no}"
            )
            sent = await asyncio.to_thread(
                send_blocking,
                campaign,
                draft["template_name"],
                draft["content"],
                targets,
                draft["subject"],
                draft["requested_by"],
                "slack",
            )
            await on_progress(render_sent(campaign, sent))
            if sent["sent"] == 0:
                break

            phones = [normalize_phone(target["to"]) for target in targets]
            resolved.update(await _poll(phones, on_progress))
            failed = await asyncio.to_thread(record_blocking, campaign, resolved)
            if not failed:
                break

            targets = failed
            if round_no < SEND_ROUNDS:
                await on_progress(f"도달 실패 {len(failed)}건을 재발송합니다.")
    except Exception as error:
        # 백그라운드 태스크라 예외가 슬랙에 안 보이면 발송이 멈춘 줄도 모릅니다.
        await on_progress(f":x: 발송 중 오류가 발생했습니다: `{error}`")
        raise

    delivered = [p for p in all_phones if resolved.get(p) == sms_result.DELIVERED]
    failed_final = [p for p in all_phones if resolved.get(p) == sms_result.FAILED]
    unknown = [p for p in all_phones if p not in resolved]

    lines = [f"*문자 발송 완료* `{draft['campaign']}`", f"• 도달 {len(delivered)}건"]
    if failed_final:
        lines.append(f"• 실패 {len(failed_final)}건 — {', '.join(failed_final)}")
    if unknown:
        lines.append(
            f"• 결과 미확정 {len(unknown)}건 — {', '.join(unknown)}\n"
            "  (중복 발송을 피하려 재발송하지 않았습니다. 뿌리오 발송결과에서 확인해 주세요)"
        )
    await on_progress("\n".join(lines))


async def _replace_card(client: AsyncWebClient, body: dict, text: str) -> None:
    """승인 카드를 결과 문구로 바꿔 버튼을 없앱니다."""
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
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
            client, body, f":x: <@{body['user']['id']}> 취소 — 발송하지 않았습니다."
        )

    @app.event("reaction_added")
    async def handle_reaction_added(body, client):
        """승인 카드에 ✅ 를 달면 승인으로 봅니다."""
        if is_duplicate_event(body):
            return

        event = body["event"]
        if event["reaction"] not in APPROVE_REACTIONS:
            return

        draft_id = _MESSAGE_TS_TO_DRAFT_ID.get(event["item"]["ts"])
        if draft_id is None:
            return

        await approve_draft(draft_id, event["user"], client)
