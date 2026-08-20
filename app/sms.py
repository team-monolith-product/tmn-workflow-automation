"""
슬랙에서 문자를 보내는 흐름입니다.

    ① 사람이 "이 번호들한테 이렇게 보내줘" 라고 말한다
    ② 에이전트가 draft_sms 도구로 초안을 올린다 (아직 안 나간다)
    ③ 요청한 사람이 [보내기] 를 누르면 그때 나간다

에이전트가 도구를 부르는 것만으로는 문자가 나가지 않습니다. 실제 사람에게
돈을 들여 나가는 것이라, 모델이 대화를 잘못 읽었을 때 되돌릴 방법이 없습니다.
사람이 전문과 대상을 눈으로 보고 누르는 단계를 반드시 거칩니다.

승인은 요청한 사람만 할 수 있습니다. 남이 누르면 그 사람 이름으로 기록되고,
무엇보다 옆에서 지나가다 잘못 누르는 경로가 열립니다.
"""

import asyncio
from typing import Any

from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.sms import draft
from service.sms import send as sms_send

APPROVE = "sms_approve"
CANCEL = "sms_cancel"

# 카드에 이름을 다 적으면 300명 캠페인에서 메시지가 잘린다.
PREVIEW_NAMES = 8


def _target_line(rows: list[dict[str, Any]]) -> str:
    """대상 요약 한 줄을 만듭니다."""
    names = [row.get("name") or row["to"] for row in rows[:PREVIEW_NAMES]]
    more = len(rows) - len(names)
    return ", ".join(names) + (f" 외 {more}명" if more > 0 else "")


def approval_blocks(item: draft.Draft, summary: dict[str, Any]) -> list[dict]:
    """승인 카드를 만듭니다.

    사람이 눌러야 하는 것은 "보낼지 말지" 하나이므로, 판단에 필요한 것만
    올립니다 — 누구에게, 무슨 내용이, 어떤 요금으로 나가는지.

    Args:
        item: 보관된 초안
        summary: sms_send.preview 결과

    Returns:
        list[dict]: Block Kit 블록
    """
    head = f"*{item.campaign or '개인 CS'}* · {summary['targets']}명"
    if summary["folded"]:
        head += f" (중복 {summary['folded']}건 접음)"
    meta = f"{summary['message_type']} · 치환 후 최대 {summary['max_bytes']}byte"
    if item.campaign is None:
        meta += " · 중복 차단 없음"

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"문자 발송 확인\n{head}\n{meta}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*대상*\n{_target_line(item.rows)}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*미리보기*\n```{summary['sample']}```",
            },
        },
        {
            "type": "actions",
            "block_id": f"sms:{item.id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "보내기"},
                    "value": item.id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "정말 보냅니다"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{summary['targets']}명에게 문자가 나갑니다. "
                            "되돌릴 수 없습니다.",
                        },
                        "confirm": {"type": "plain_text", "text": "보내기"},
                        "deny": {"type": "plain_text", "text": "취소"},
                    },
                },
                {
                    "type": "button",
                    "action_id": CANCEL,
                    "text": {"type": "plain_text", "text": "취소"},
                    "value": item.id,
                },
            ],
        },
    ]


def get_sms_tools(
    client: AsyncWebClient, user_id: str | None, channel: str, thread_ts: str
) -> list:
    """문자 발송 초안 도구를 반환합니다.

    요청자와 채널을 도구 인자가 아니라 클로저로 받습니다. 인자로 두면 모델이
    남의 이름으로 발송을 올릴 수 있습니다.

    Args:
        client: 슬랙 클라이언트
        user_id: 요청한 사람의 Slack 사용자 ID
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프

    Returns:
        list: [초안 도구]
    """

    @tool
    async def draft_sms(
        content: str,
        targets: list[dict],
        campaign: str | None = None,
    ) -> str:
        """
        문자 발송 초안을 스레드에 올립니다. 이 도구는 문자를 보내지 않습니다.
        사람이 카드의 [보내기] 를 눌러야 그때 나갑니다.

        content 는 보낼 문안입니다. 치환이 필요하면 뿌리오 태그를 씁니다 —
        받는 사람 이름은 [*이름*], 나머지는 [*1*]~[*8*].

        targets 는 수신자 목록입니다. 각 항목은 to(번호)가 필수이고,
        name·var1~var8 로 치환값을 줍니다.
        예: [{"to": "010-1111-1111", "name": "홍길동", "var1": "1기"}]

        campaign 은 발송 건 식별자입니다. 공식 안내처럼 같은 사람에게 두 번
        가면 안 되는 발송에 붙이면, 같은 campaign 으로 다시 보낼 때 이미 받은
        사람이 자동으로 빠집니다. 개인 CS 처럼 여러 번 보내는 게 정상이면
        비워 둡니다.

        Returns:
            초안을 올렸다는 안내. 발송 결과가 아닙니다.
        """
        if not user_id:
            return "요청자를 알 수 없어 초안을 올리지 않았습니다."

        problems = sms_send.check(targets, content=content)
        if problems:
            return "보내기 전에 고칠 것: " + " / ".join(problems)

        summary = sms_send.preview(targets, None, content)
        item = draft.put(
            campaign=campaign,
            content=content,
            rows=targets,
            requested_by=user_id,
            channel_id=channel,
        )
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"문자 발송 확인 — {summary['targets']}명",
            blocks=approval_blocks(item, summary),
        )
        return (
            f"{summary['targets']}명 대상 초안을 올렸습니다. "
            "카드에서 [보내기] 를 눌러야 실제로 나갑니다."
        )

    return [draft_sms]


def register_sms_handlers(app):
    """승인·취소 버튼 핸들러를 등록합니다.

    Args:
        app: slack_bolt AsyncApp
    """

    @app.action(APPROVE)
    async def approve(ack, body, client):
        await ack()
        draft_id = body["actions"][0]["value"]
        who = body["user"]["id"]
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]

        item = draft.take(draft_id)
        if item is None:
            await _replace(client, channel, ts, "만료됐거나 이미 처리된 초안입니다.")
            return
        if who != item.requested_by:
            # 되돌릴 수 없는 발송이라 요청한 사람만 누를 수 있다.
            draft.restore(item)
            await client.chat_postEphemeral(
                channel=channel,
                user=who,
                text=f"<@{item.requested_by}> 님이 올린 초안이라 그분만 보낼 수 있습니다.",
            )
            return

        try:
            result = await asyncio.to_thread(
                sms_send.send_campaign,
                campaign=item.campaign,
                rows=item.rows,
                content=item.content,
                channel_id=item.channel_id,
                requested_by=who,
            )
        except Exception as error:
            # 삼키지 않는다. 접수 여부를 모르는 실패는 사람이 뿌리오 웹에서
            # 확인해야 하므로 무엇이 터졌는지 그대로 보여준다.
            await _replace(
                client,
                channel,
                ts,
                f"발송에 실패했습니다 — {type(error).__name__}: {error}",
            )
            raise

        done = f"<@{who}> 님이 보냈습니다 — {result['sent']}명"
        if result["skipped"]:
            done += f" · 이미 받은 {result['skipped']}명 제외"
        if result["message_key"]:
            done += f"\nmessageKey `{result['message_key']}`"
        await _replace(client, channel, ts, done)

    @app.action(CANCEL)
    async def cancel(ack, body, client):
        await ack()
        draft.drop(body["actions"][0]["value"])
        await _replace(
            client,
            body["container"]["channel_id"],
            body["container"]["message_ts"],
            f"<@{body['user']['id']}> 님이 취소했습니다. 아무것도 보내지 않았습니다.",
        )


async def _replace(client, channel: str, ts: str, text: str) -> None:
    """카드를 결과 문구로 바꿉니다. 버튼이 남아 있으면 또 눌린다."""
    await client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
