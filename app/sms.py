"""
슬랙에서 문자를 보내는 흐름입니다.

    ① 사람이 "이 번호들한테 이렇게 보내줘" 라고 말한다
    ② 에이전트가 draft_sms 로 초안 카드를 올린다 (아직 안 나간다)
    ③ [보내기] 를 누르면 그때 나간다

도구를 부르는 것만으로는 나가지 않습니다. 실제 사람에게 나가는 것이라
모델이 대화를 잘못 읽었을 때 되돌릴 방법이 없습니다.
"""

import asyncio
import uuid
from typing import Any

from cachetools import TTLCache
from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.sms import send as sms_send
from service.sms import transport

APPROVE = "sms_approve"
CANCEL = "sms_cancel"

# 슬랙 버튼 value 는 2000자 제한이라 수신자 목록을 통째로 못 싣는다.
# 초안은 여기 두고 버튼에는 id 만 싣는다.
_DRAFTS: TTLCache = TTLCache(maxsize=200, ttl=3600)


def _blocks(draft_id: str, summary: dict[str, Any]) -> list[dict]:
    """승인 카드를 만듭니다."""
    head = f"{summary['targets']}명 · {summary['message_type']}"
    if summary["folded"]:
        head += f" · 중복 {summary['folded']}건 접음"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*문자 발송 확인* — {head}\n```{summary['sample']}```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "보내기"},
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "action_id": CANCEL,
                    "text": {"type": "plain_text", "text": "취소"},
                    "value": draft_id,
                },
            ],
        },
    ]


def get_sms_tools(client: AsyncWebClient, channel: str, thread_ts: str) -> list:
    """문자 발송 초안 도구를 반환합니다.

    채널을 도구 인자가 아니라 클로저로 받습니다.

    Args:
        client: 슬랙 클라이언트
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프

    Returns:
        list: [초안 도구]
    """

    @tool
    async def draft_sms(content: str, targets: list[dict]) -> str:
        """
        문자 발송 초안을 스레드에 올립니다. 이 도구는 문자를 보내지 않습니다.
        사람이 카드의 [보내기] 를 눌러야 그때 나갑니다.

        content 는 보낼 문안입니다. 치환이 필요하면 뿌리오 태그를 씁니다 —
        받는 사람 이름은 [*이름*], 나머지는 [*1*]~[*8*].

        targets 는 수신자 목록입니다. to(번호)가 필수이고 name·var1~var8 로
        치환값을 줍니다.
        예: [{"to": "010-1111-1111", "name": "홍길동", "var1": "1기"}]

        Returns:
            초안을 올렸다는 안내. 발송 결과가 아닙니다.
        """
        summary = sms_send.preview(targets, content)
        if summary["problems"]:
            return "보내기 전에 고칠 것: " + " / ".join(summary["problems"])

        draft_id = uuid.uuid4().hex[:12]
        _DRAFTS[draft_id] = {"rows": targets, "content": content}
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"문자 발송 확인 — {summary['targets']}명",
            blocks=_blocks(draft_id, summary),
        )
        return f"{summary['targets']}명 대상 초안을 올렸습니다. [보내기] 를 눌러주세요."

    return [draft_sms]


def register_sms_handlers(app):
    """승인·취소 버튼 핸들러를 등록합니다.

    Args:
        app: slack_bolt AsyncApp
    """

    @app.action(APPROVE)
    async def approve(ack, body, client):
        await ack()
        # 꺼내면서 지운다. 두 번 눌러도 벤더를 두 번 부르지 않는다.
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]
        if draft is None:
            await _replace(client, channel, ts, "이미 처리된 초안입니다.")
            return

        try:
            result = await asyncio.to_thread(
                sms_send.send, rows=draft["rows"], content=draft["content"]
            )
        except transport.PpurioError as error:
            await _replace(client, channel, ts, f"안 나갔습니다 — {error}")
            raise
        except Exception as error:
            # 타임아웃·5xx 는 뿌리오가 이미 접수했을 수 있다. "실패" 라고 하면
            # 다시 보내고, 같은 사람이 두 번 받는다.
            await _replace(
                client,
                channel,
                ts,
                f"접수 여부를 모릅니다 ({type(error).__name__})"
                " — 뿌리오 웹에서 확인하고 보내세요",
            )
            raise

        await _replace(
            client,
            channel,
            ts,
            f"<@{body['user']['id']}> 님이 보냈습니다 — {result['sent']}명"
            f" (messageKey `{result['message_key']}`)",
        )

    @app.action(CANCEL)
    async def cancel(ack, body, client):
        await ack()
        # 이미 발송이 시작된 초안을 "취소했습니다" 로 덮으면, 누른 사람은
        # 막았다고 믿는데 문자는 나가는 중이다.
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        await _replace(
            client,
            body["container"]["channel_id"],
            body["container"]["message_ts"],
            (
                f"<@{body['user']['id']}> 님이 취소했습니다."
                if draft
                else "이미 처리된 초안입니다."
            ),
        )


async def _replace(client, channel: str, ts: str, text: str) -> None:
    """카드를 결과 문구로 바꿉니다. 버튼이 남아 있으면 또 눌린다."""
    await client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
