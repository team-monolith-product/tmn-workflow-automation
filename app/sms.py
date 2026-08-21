"""
슬랙에서 문자를 보내는 흐름입니다.

    ① 사람이 "이 번호들한테 이렇게 보내줘" 라고 말한다
    ② 에이전트가 draft_sms 로 초안 카드를 올린다 (아직 안 나간다)
    ③ [보내기] 를 누르면 그때 나간다 — 예약이면 그때 벤더에 예약이 걸린다
       고칠 데가 있으면 [수정] 으로 어디를 어떻게 고칠지 적어 낸다.
       취소하고 처음부터 다시 말할 필요가 없다

도구를 부르는 것만으로는 나가지 않습니다. 실제 사람에게 나가는 것이라
모델이 대화를 잘못 읽었을 때 되돌릴 방법이 없습니다.
"""

import asyncio
import json
import uuid
from datetime import datetime

from cachetools import TTLCache
from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.sms import send as sms_send
from service.sms import transport

APPROVE = "sms_approve"
CANCEL = "sms_cancel"
REVISE = "sms_revise"
REVISE_VIEW = "sms_revise_view"
FEEDBACK_BLOCK = "sms_feedback"
FEEDBACK_INPUT = "sms_feedback_input"

# 슬랙 버튼 value 는 2000자 제한이라 수신자 목록을 통째로 못 싣는다.
# 초안은 여기 두고 버튼에는 id 만 싣는다.
_DRAFTS: TTLCache = TTLCache(maxsize=200, ttl=3600)

# 줄 수와 줄 폭. 22줄 × 121자면 슬랙 section text 3000자에 든다.
MAX_ROWS = 20
MAX_WIDTH = 120


def _value_table(targets: list[dict]) -> str:
    """치환값 목록을 표로 만듭니다. 벤더로 나가는 targets 를 그대로 씁니다.

    접을 때 마지막 한 줄은 남깁니다. 이름이 한 칸씩 밀리는 사고는 앞줄만
    보면 안 보이고 끝에서 티가 납니다.
    """
    # targets 의 키는 문안의 태그로 정해지므로 전 행이 같다. 첫 행이 대표값이다.
    first = targets[0]
    change = list(first.get("changeWord", {}))
    named = "name" in first
    head = ["번호", *(["이름"] if named else []), *(f"[*{k[3:]}*]" for k in change)]

    def line(target: dict) -> str:
        cells = [
            target["to"],
            *([target["name"] or "-"] if named else []),
            *(target["changeWord"][key] or "-" for key in change),
        ]
        # 값에 줄 나눔이 있으면 한 행이 두 줄로 쪼개져 딱 "한 칸 밀림" 처럼
        # 보이고, 백틱이 있으면 코드펜스가 거기서 닫혀 표가 무너진다.
        # \r 도 줄을 나눈다 — \n 만 막으면 \r\n 값에서 그대로 재현된다.
        text = "  ".join(cells)
        for char in "\r\n":
            text = text.replace(char, " ")
        text = text.replace("`", "'")
        return text if len(text) <= MAX_WIDTH else text[: MAX_WIDTH - 1] + "…"

    lines = ["  ".join(head)]
    if len(targets) <= MAX_ROWS:
        return "\n".join(lines + [line(target) for target in targets])
    return "\n".join(
        lines
        + [line(target) for target in targets[: MAX_ROWS - 1]]
        + [f"… {len(targets) - MAX_ROWS}명 접음 …", line(targets[-1])]
    )


def _when(send_time: str | None) -> str:
    """예약 시각을 사람이 읽는 꼴로. 즉시 발송이면 빈 문자열입니다.

    시간대를 붙여 씁니다. 해외에 있거나 노트북 시계가 딴 데 맞춰져 있으면
    "09:00" 이 어느 나라 9시인지 물어보게 됩니다.
    """
    if not send_time:
        return ""
    when = datetime.strptime(send_time, sms_send.SEND_TIME_FORMAT)
    days = "월화수목금토일"[when.weekday()]
    return f"{when.month}/{when.day}({days}) {when:%H:%M} KST"


def _blocks(draft_id: str, plan: sms_send.Plan) -> list[dict]:
    """승인 카드를 만듭니다. 치환 후 문장이 아니라 태그가 살아 있는 원문입니다."""
    head = f"{len(plan.rows)}명 · {plan.message_type}"
    if plan.folded:
        head += f" · 중복 {plan.folded}건 접음"
    # 예약은 헤더 맨 앞에 둡니다. 뒤에 붙이면 중복 접음 문구에 묻혀,
    # 지금 나갈 문자로 읽고 눌러 버립니다.
    if plan.send_time:
        head = f"⏰ {_when(plan.send_time)} 예약 · " + head
    # 문안에 백틱이 있으면 펜스가 거기서 닫혀 나머지가 mrkdwn 으로 렌더된다.
    # 그러면 [*이름*] 이 굵은 글씨가 되어, 실명이 박힌 사고와 화면상 구분이
    # 안 된다. 개행은 문안에서 의미가 있으므로 건드리지 않는다.
    body = plan.template.replace("`", "'")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*문자 발송 확인* — {head}\n```{body}```",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*치환값*\n```{_value_table(plan.targets)}```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVE,
                    "style": "primary",
                    # 예약인데 "보내기" 라고 쓰면 지금 나가는 줄 알고 누릅니다.
                    "text": {
                        "type": "plain_text",
                        "text": "예약하기" if plan.send_time else "보내기",
                    },
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "action_id": REVISE,
                    "text": {"type": "plain_text", "text": "수정"},
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
    async def draft_sms(content: str, targets: list[dict], send_at: str = "") -> str:
        """
        문자 발송 초안을 스레드에 올립니다. 이 도구는 문자를 보내지 않습니다.
        사람이 카드의 [보내기] 를 눌러야 그때 나갑니다.

        content 는 보낼 문안입니다. 치환이 필요하면 뿌리오 태그를 씁니다 —
        받는 사람 이름은 [*이름*], 나머지는 [*1*]~[*8*].

        targets 는 수신자 목록입니다. to(번호)가 필수이고 name·var1~var8 로
        치환값을 줍니다.
        예: [{"to": "010-1111-1111", "name": "홍길동", "var1": "1기"}]

        send_at 은 예약 시각입니다. 비우면 승인 즉시 나갑니다.
        "내일 아침 9시" 처럼 말하면 한국 시간 기준으로 날짜를 계산해
        "2026-08-22 09:00" 꼴로 넘깁니다. 지금부터 3분 뒤부터 지정할 수 있습니다.

        Returns:
            초안을 올렸다는 안내. 발송 결과가 아닙니다.
        """
        plan = sms_send.preview(targets, content, send_at)
        if plan.problems:
            return "보내기 전에 고칠 것: " + " / ".join(plan.problems)

        draft_id = uuid.uuid4().hex[:12]
        _DRAFTS[draft_id] = {
            "rows": targets,
            "content": content,
            "send_at": send_at,
        }
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"문자 발송 확인 — {len(plan.rows)}명",
            blocks=_blocks(draft_id, plan),
        )
        if plan.send_time:
            return (
                f"{len(plan.rows)}명 대상 초안을 올렸습니다."
                f" {_when(plan.send_time)} 발송으로 예약하려면"
                " [예약하기] 를 눌러주세요."
            )
        return f"{len(plan.rows)}명 대상 초안을 올렸습니다. [보내기] 를 눌러주세요."

    return [draft_sms]


def _revise_view(draft_id: str, channel: str, ts: str, plan: sms_send.Plan) -> dict:
    """수정 요청 모달입니다. 고칠 것을 옆에 두고 피드백을 받습니다.

    **문안만이 아니라 수신자·치환값·예약 시각도 같이 보여줍니다.** 고칠 수 있는
    것을 안 보여주면 고칠 수 있는 줄 모릅니다 — 명단에서 한 명 빼는 것도
    피드백으로 되는데, 문안만 떠 있으면 취소하고 처음부터 다시 말하게 됩니다.
    빼려는 사람이 목록 어디에 있는지도 보여야 "그 사람" 을 지목할 수 있습니다.

    직접 고치게 하지 않고 피드백만 받습니다. 치환 태그가 섞인 원문을 손으로
    고치면 태그가 깨지기 쉽고, 깨진 태그는 실명 자리에 그대로 나갑니다.
    번호와 치환값의 짝도 손으로 만지면 한 칸씩 밀립니다.
    고쳐 쓰는 것은 초안을 쓴 에이전트에게 맡깁니다.
    """
    return {
        "type": "modal",
        "callback_id": REVISE_VIEW,
        "private_metadata": json.dumps(
            {"draft_id": draft_id, "channel": channel, "ts": ts}
        ),
        "title": {"type": "plain_text", "text": "문자 수정 요청"},
        "submit": {"type": "plain_text", "text": "다시 쓰기"},
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        (f"⏰ {_when(plan.send_time)} 예약\n" if plan.send_time else "")
                        + f"```{plan.template.replace('`', chr(39))}```"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*받는 사람* {len(plan.rows)}명\n"
                        f"```{_value_table(plan.targets)}```"
                    ),
                },
            },
            {
                "type": "input",
                "block_id": FEEDBACK_BLOCK,
                "label": {"type": "plain_text", "text": "어디를 어떻게 고칠까요?"},
                "hint": {
                    "type": "plain_text",
                    "text": "문안·받는 사람·예약 시각 무엇이든 됩니다.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": FEEDBACK_INPUT,
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "예) 마감일을 8월 30일로 고치고, 김철수 선생님은 빼주세요",
                    },
                },
            },
        ],
    }


def register_sms_handlers(app, revise=None):
    """승인·수정·취소 버튼 핸들러를 등록합니다.

    Args:
        app: slack_bolt AsyncApp
        revise: 수정 피드백을 초안을 쓴 에이전트에게 되돌리는 콜백.
            `async (channel, thread_ts, user, text) -> None` 입니다.
            없으면 [수정] 은 피드백을 스레드에 남기기만 합니다 —
            에이전트 진입점을 아는 것은 app.py 뿐이라 여기서 import 하면
            general → sms → general 로 순환합니다.
    """

    @app.action(APPROVE)
    async def approve(ack, body, client):
        await ack()
        # 꺼내면서 지운다. 두 번 눌러도 벤더를 두 번 부르지 않는다.
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        if draft is None:
            return
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]

        try:
            result = await asyncio.to_thread(
                sms_send.send,
                rows=draft["rows"],
                content=draft["content"],
                send_at=draft.get("send_at", ""),
            )
        except ValueError as error:
            # 승인까지 기다리는 사이에 예약 시각이 지나간 경우가 여기로 온다.
            # 아래 "접수 여부를 모릅니다" 로 새면, 안 나간 것을 나갔을 수도 있다고
            # 읽어 아무도 다시 보내지 않는다.
            await _replace(client, channel, ts, f"안 나갔습니다 — {error}")
            return
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

        done = (
            f"{_when(result['send_time'])} 발송으로 예약했습니다"
            if result.get("send_time")
            else "보냈습니다"
        )
        await _replace(
            client,
            channel,
            ts,
            f"<@{body['user']['id']}> 님이 {done} — {result['sent']}명"
            f" (messageKey `{result['message_key']}`)",
        )

    @app.action(REVISE)
    async def open_revise(ack, body, client):
        await ack()
        draft_id = body["actions"][0]["value"]
        draft = _DRAFTS.get(draft_id)
        # 여기서는 꺼내지 않는다. 모달을 열어 놓고 닫아 버릴 수 있고,
        # 그때 초안이 사라지면 멀쩡한 카드의 [보내기] 가 죽는다.
        if draft is None:
            return
        # 예약 초안이면 예약도 함께 넘긴다. 안 넘기면 모달이 예약을 모른 채
        # 문안만 보여줘, 언제 나가는 건지 모르고 피드백을 적게 된다.
        plan = sms_send.preview(
            draft["rows"], draft["content"], draft.get("send_at", "")
        )
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=_revise_view(
                draft_id,
                body["container"]["channel_id"],
                body["container"]["message_ts"],
                plan,
            ),
        )

    @app.view(REVISE_VIEW)
    async def submit_revise(ack, body, client):
        await ack()
        meta = json.loads(body["view"]["private_metadata"])
        feedback = body["view"]["state"]["values"][FEEDBACK_BLOCK][FEEDBACK_INPUT][
            "value"
        ]
        # 초안을 여기서 버린다. 이 카드의 [보내기] 는 이제 옛 문안이라
        # 눌리면 안 된다. 새 문안은 에이전트가 새 카드로 올린다.
        if _DRAFTS.pop(meta["draft_id"], None) is None:
            return
        user = body["user"]["id"]
        quoted = "\n".join(f"&gt; {line}" for line in feedback.splitlines())
        await _replace(
            client,
            meta["channel"],
            meta["ts"],
            f"<@{user}> 님이 수정을 요청했습니다.\n{quoted}",
        )
        if revise is None:
            return
        # 스레드 타임스탬프는 카드가 달린 스레드다. 카드는 항상 스레드 안에 올린다.
        thread = await client.conversations_replies(
            channel=meta["channel"], ts=meta["ts"], limit=1
        )
        thread_ts = thread["messages"][0].get("thread_ts", meta["ts"])
        await revise(
            channel=meta["channel"], thread_ts=thread_ts, user=user, text=feedback
        )

    @app.action(CANCEL)
    async def cancel(ack, body, client):
        await ack()
        # 이미 처리된 초안이면 결과 카드를 덮지 않는다. 발송이 시작됐는데
        # "취소했습니다" 로 덮으면 누른 사람은 막았다고 믿고, 발송이 끝난
        # 뒤라면 이 PR 의 유일한 기록인 messageKey 가 지워진다.
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        if draft is None:
            return
        await _replace(
            client,
            body["container"]["channel_id"],
            body["container"]["message_ts"],
            f"<@{body['user']['id']}> 님이 취소했습니다.",
        )


async def _replace(client, channel: str, ts: str, text: str) -> None:
    """카드를 결과 문구로 바꿉니다. 버튼이 남아 있으면 또 눌린다."""
    await client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
