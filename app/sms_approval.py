"""
문자 발송 승인·추적 핸들러입니다.

    draft_sms → 승인 카드 → [발송] 버튼 또는 ✅ → 발송 → 도달 확인 → 실패분 재발송 → 보고

대외 발신이라 사람이 누르기 전에는 아무것도 나가지 않습니다. 슬랙 에이전트가
부를 수 있는 도구에는 발송 경로가 없습니다.

재발송은 campaign 을 -r2, -r3 으로 바꿔 부릅니다. 발송이력 시트가 (캠페인, 번호)
단위로 승자를 가리므로, 캠페인 이름이 달라지면 새로 자리를 잡을 수 있고 이미
도달한 사람은 애초에 재발송 대상에 안 들어갑니다.
"""

import asyncio
import datetime
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from cachetools import TTLCache
from slack_sdk.web.async_client import AsyncWebClient

from app.event_dedup import is_duplicate_event
from app.sms_render import render_sent
from service.sms import KST
from service.sms import result as sms_result
from service.sms import send as sms_send
from service.sms.templates import normalize_phone

APPROVE_REACTIONS = {
    "white_check_mark",
    "heavy_check_mark",
    "ballot_box_with_check",
    "o",
    "ok",
}

# 자동 재발송은 1회로 둡니다. 재발송 판정이 뿌리오 웹 페이지 파싱에 걸려
# 있는데 그 파싱은 아직 실계정으로 검증된 적이 없습니다. 잘못 읽으면 도달한
# 사람에게 또 보내고, 그건 대외 발신이라 되돌릴 수 없습니다. 실패분은 보고만
# 하고 사람이 승인 카드로 다시 보냅니다. 파서를 실물로 보정한 뒤 올리세요.
SEND_ROUNDS = 1
POLL_INTERVAL_SECONDS = 60
POLL_LIMIT = 5  # 라운드당 최대 5회까지 도달 결과를 기다린다

# 서버 시계와 뿌리오 웹 표기 시각이 조금 어긋나도 이번 발송분을 놓치지 않도록
# 뒤로 물리는 여유입니다.
SENT_AFTER_MARGIN = datetime.timedelta(minutes=2)
ROSTER_PREVIEW_LIMIT = 20

# 승인 대기 초안은 프로세스 메모리에만 둡니다. 봇이 재시작하면 초안이 사라져
# 다시 요청해야 하지만, 발송 사실은 발송이력 시트에 남으므로 문자가 새지는 않습니다.
_DRAFTS: TTLCache = TTLCache(maxsize=100, ttl=86400)
_MESSAGE_TS_TO_DRAFT_ID: TTLCache = TTLCache(maxsize=100, ttl=86400)

# create_task 참조를 유지하여 GC 방지
_background_tasks: set[asyncio.Task] = set()

Progress = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Draft:
    """승인 대기 중인 발송 한 건."""

    campaign: str
    targets: list[dict]
    requested_by: str
    channel: str
    thread_ts: str
    spreadsheet_id: str
    template_name: str | None = None
    content: str | None = None
    subject: str | None = None
    # 초안마다 새로 뽑습니다. 승인·만료로 _DRAFTS 가 줄어들기 때문에 개수를
    # 세어 붙이면 옛 카드와 새 초안이 같은 id 를 갖고, 옛 카드에 달린 ✅ 가
    # 엉뚱한 초안을 승인합니다.
    id: str = field(default_factory=lambda: secrets.token_hex(4))

    @property
    def source(self) -> str:
        """문안 출처를 한 줄로 설명합니다."""
        return (
            f"문안 파일 `{self.template_name}`" if self.template_name else "즉석 문안"
        )


def _build_blocks(draft: Draft, summary: dict) -> list[dict]:
    """승인 카드 블록을 만듭니다.

    Args:
        draft: 발송 초안
        summary: send.preview 결과

    Returns:
        list[dict]: 슬랙 blocks
    """
    roster = "\n".join(
        f"{index}. {target.get('name', '')} {target['to']}"
        for index, target in enumerate(draft.targets[:ROSTER_PREVIEW_LIMIT], 1)
    )
    if len(draft.targets) > ROSTER_PREVIEW_LIMIT:
        roster += f"\n… 외 {len(draft.targets) - ROSTER_PREVIEW_LIMIT}명"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "문자 발송 승인 요청"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*`{draft.campaign}` · 수신자 {len(draft.targets)}명*\n{roster}",
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
                        f"{draft.source} · {summary['message_type']} · 치환 후 최대 "
                        f"{summary['max_bytes']}byte · 초안 `{draft.id}`\n"
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
                    "value": draft.id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "문자를 발송할까요?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"수신자 {len(draft.targets)}명에게 즉시 발송됩니다.",
                        },
                        "confirm": {"type": "plain_text", "text": "발송"},
                        "deny": {"type": "plain_text", "text": "취소"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "취소"},
                    "action_id": "sms_cancel",
                    "value": draft.id,
                },
            ],
        },
    ]


async def post_draft(client: AsyncWebClient, draft: Draft, summary: dict) -> str:
    """승인 카드를 올리고 초안을 등록합니다.

    Args:
        client: 슬랙 클라이언트
        draft: 발송 초안
        summary: send.preview 결과

    Returns:
        str: 에이전트에게 돌려줄 안내
    """
    response = await client.chat_postMessage(
        channel=draft.channel,
        thread_ts=draft.thread_ts,
        text=f"문자 발송 승인 요청 ({draft.campaign} · {len(draft.targets)}명)",
        blocks=_build_blocks(draft, summary),
    )
    _DRAFTS[draft.id] = draft
    _MESSAGE_TS_TO_DRAFT_ID[response["ts"]] = draft.id

    return (
        f"승인 카드를 올렸습니다. 아직 발송하지 않았습니다.\n"
        f"`{draft.campaign}` · 수신자 {len(draft.targets)}명 · "
        f"{summary['message_type']} 최대 {summary['max_bytes']}byte\n"
        "답변에는 명단과 문안을 정리해 보여주고, [발송] 버튼이나 ✅ 로 승인해 달라고 안내하세요."
    )


async def approve_draft(
    draft_id: str, approver: str | None, client: AsyncWebClient
) -> tuple[bool, str]:
    """초안을 승인하고 발송·추적을 백그라운드로 시작합니다.

    Args:
        draft_id: 초안 식별자
        approver: 승인한 슬랙 사용자 ID
        client: 슬랙 클라이언트

    Returns:
        tuple[bool, str]: (승인됐는가, 카드에 적을 문구). 성공 여부를 같이
            돌려주지 않으면 호출부가 실패에도 "✅ 승인" 을 붙여, 누른 사람이
            발송된 줄 안다
    """
    draft = _DRAFTS.pop(draft_id, None)
    if draft is None:
        return False, "이미 처리되었거나 만료된 초안입니다."

    task = asyncio.create_task(_send_and_report(draft, approver, client))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return True, f"{len(draft.targets)}명에게 발송을 시작합니다."


async def _poll(
    phones: list[str], sent_after: datetime.datetime, on_progress: Progress
) -> dict[str, str]:
    """모든 번호의 도달 결과가 확정될 때까지 웹 발송결과를 읽습니다.

    확정분은 누적합니다. 매번 대입하면 페이지가 일부만 돌려줬을 때 앞서 확정된
    결과를 잃습니다. 첫 조회는 기다리지 않고 바로 합니다 — 이미 결과가 올라와
    있으면 그대로 끝납니다.

    Args:
        phones: 조회할 번호 목록. 이번에 실제로 나간 번호만 넘겨야 한다
        sent_after: 이 시각 이후 발송분만 본다
        on_progress: 진행 보고 콜백

    Returns:
        dict[str, str]: 확정된 {번호: 결과코드}
    """
    statuses: dict[str, str] = {}
    for attempt in range(1, POLL_LIMIT + 1):
        statuses.update(await sms_result.fetch_results(phones, sent_after))
        await on_progress(
            f"도달 확인 {attempt}/{POLL_LIMIT} — 확정 {len(statuses)}/{len(phones)}건"
        )
        if len(statuses) == len(phones):
            break
        if attempt < POLL_LIMIT:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return statuses


async def _run_rounds(draft: Draft, on_progress: Progress) -> dict[str, str]:
    """발송하고 실패분을 재발송합니다.

    라운드마다 그 라운드에서 확정된 결과만 기록합니다. 누적분을 넘기면 앞
    라운드의 실패가 이번 라운드 행에 찍혀, 실제로는 성공했는데 실패로 남습니다.

    Args:
        draft: 발송 초안
        on_progress: 진행 보고 콜백

    Returns:
        dict[str, str]: 전체 라운드에서 확정된 {번호: 결과코드}
    """
    resolved: dict[str, str] = {}
    targets = draft.targets

    for round_no in range(1, SEND_ROUNDS + 1):
        campaign = draft.campaign if round_no == 1 else f"{draft.campaign}-r{round_no}"
        # 페이지 일시는 KST 벽시계다. 컨테이너의 now() 는 UTC 라 그대로 쓰면
        # 최근 9시간이 전부 '이번 발송분'으로 통과한다.
        sent_after = datetime.datetime.now(KST).replace(tzinfo=None) - SENT_AFTER_MARGIN
        sent = await asyncio.to_thread(
            sms_send.send_campaign,
            spreadsheet_id=draft.spreadsheet_id,
            campaign=campaign,
            rows=targets,
            template_name=draft.template_name,
            content=draft.content,
            subject=draft.subject,
            requested_by=draft.requested_by,
            entrypoint="slack",
        )
        await on_progress(render_sent(campaign, sent))
        if sent["sent"] == 0:
            break

        # 이미 보낸 번호는 send_campaign 이 걸러내므로 대상 전체를 기다리면
        # 오지 않을 결과를 붙들고 폴링을 끝까지 돌린다.
        round_statuses = await _poll(sent["sent_to"], sent_after, on_progress)
        resolved.update(round_statuses)

        failed = await asyncio.to_thread(
            sms_result.record, draft.spreadsheet_id, campaign, round_statuses
        )
        if not failed:
            break

        # failed 는 {"to", "name"} 뿐이라 그대로 쓰면 재발송 문안의
        # [*1*]~[*8*] 이 전부 빈 문자열로 치환돼 나간다. 원본 행을 되찾는다.
        by_phone = {normalize_phone(row["to"]): row for row in draft.targets}
        targets = [by_phone.get(normalize_phone(f["to"]), f) for f in failed]
        if round_no < SEND_ROUNDS:
            await on_progress(f"도달 실패 {len(failed)}건을 재발송합니다.")

    return resolved


def _render_report(campaign: str, phones: list[str], resolved: dict[str, str]) -> str:
    """최종 보고 문구를 만듭니다.

    Args:
        campaign: 발송 건 식별자
        phones: 최초 대상 전체
        resolved: 확정된 {번호: 결과코드}

    Returns:
        str: 슬랙에 올릴 보고
    """
    delivered = [p for p in phones if resolved.get(p) == sms_result.DELIVERED]
    failed = [p for p in phones if resolved.get(p) == sms_result.FAILED]
    unknown = [p for p in phones if p not in resolved]

    lines = [f"*문자 발송 완료* `{campaign}`", f"• 도달 {len(delivered)}건"]
    if failed:
        lines.append(f"• 도달 실패 {len(failed)}건 — {', '.join(failed)}")
    if unknown:
        lines.append(
            f"• 결과 미확정 {len(unknown)}건 — {', '.join(unknown)}\n"
            "  (중복 발송을 피하려 재발송하지 않았습니다. 뿌리오 발송결과에서 확인해 주세요)"
        )
    return "\n".join(lines)


async def _send_and_report(
    draft: Draft, approver: str | None, client: AsyncWebClient
) -> None:
    """발송하고 도달 결과가 확정될 때까지 추적한 뒤 스레드에 보고합니다."""

    async def on_progress(text: str) -> None:
        await client.chat_postMessage(
            channel=draft.channel, thread_ts=draft.thread_ts, text=text
        )

    approver_label = f"<@{approver}>" if approver else "요청자"
    await on_progress(
        f"{approver_label} 승인 — 발송을 시작합니다. (대상 {len(draft.targets)}명)"
    )

    phones = [normalize_phone(target["to"]) for target in draft.targets]
    try:
        resolved = await _run_rounds(draft, on_progress)
    except Exception as error:
        # 백그라운드 태스크라 예외가 슬랙에 안 보이면 발송이 멈춘 줄도 모릅니다.
        await on_progress(f":x: 발송 중 오류가 발생했습니다: `{error}`")
        raise

    await on_progress(_render_report(draft.campaign, phones, resolved))


async def _replace_card(client: AsyncWebClient, body: dict, text: str) -> None:
    """승인 카드를 결과 문구로 바꿔 버튼을 없앱니다."""
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


def register_sms_handlers(app: Any) -> None:
    """문자 발송 승인 관련 슬랙 핸들러를 등록합니다.

    Args:
        app: AsyncApp 인스턴스
    """

    @app.action("sms_send")
    async def handle_sms_send(ack, body, client):
        """[발송] 버튼 클릭"""
        await ack()
        approver = body["user"]["id"]
        approved, message = await approve_draft(
            body["actions"][0]["value"], approver, client
        )
        # 승인된 카드는 리액션 경로에서 빼둔다. 남겨두면 누군가 확인 표시로
        # ✅ 를 달았을 때 "이미 처리된 초안" 문구가 승인 기록을 덮어쓴다.
        _MESSAGE_TS_TO_DRAFT_ID.pop(body["message"]["ts"], None)
        mark = ":white_check_mark:" if approved else ":warning:"
        label = f"<@{approver}> 승인 — " if approved else ""
        await _replace_card(client, body, f"{mark} {label}{message}")

    @app.action("sms_cancel")
    async def handle_sms_cancel(ack, body, client):
        """[취소] 버튼 클릭"""
        await ack()
        _DRAFTS.pop(body["actions"][0]["value"], None)
        # 카드 ts -> 초안 매핑도 지운다. 남겨두면 취소한 카드에 달린 ✅ 가
        # 승인 경로로 들어가 조용히 아무 일도 일어나지 않는다.
        _MESSAGE_TS_TO_DRAFT_ID.pop(body["message"]["ts"], None)
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

        if event["item"]["type"] != "message":
            # 파일에 단 리액션에는 ts 가 없다.
            return

        # pop 이다. 이 카드는 이제 승인 대상이 아니므로, 뒤이어 달리는 ✅ 가
        # 승인 문구를 덮어쓰지 않는다.
        draft_id = _MESSAGE_TS_TO_DRAFT_ID.pop(event["item"]["ts"], None)
        if draft_id is None:
            return

        approved, message = await approve_draft(draft_id, event["user"], client)
        # 리액션 경로도 결과를 알려야 한다. 버리면 만료된 카드에 ✅ 를 단
        # 사람은 발송된 줄 안다.
        mark = ":white_check_mark:" if approved else ":warning:"
        label = f"<@{event['user']}> 승인 — " if approved else ""
        await client.chat_update(
            channel=event["item"]["channel"],
            ts=event["item"]["ts"],
            text=f"{mark} {label}{message}",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{mark} {label}{message}"},
                }
            ],
        )
