"""
슬랙에서 ops@team-mono.com 이름으로 메일을 보내는 흐름입니다.

    ① 메일 채널의 상대방 스레드에서 "이렇게 답장해줘" 라고 말한다
       (스레드 밖에서는 받는 사람과 제목을 함께 말하면 새 메일.
       받는 사람이 여럿이면 한 명씩 따로 보낸다)
       파일을 붙이려면 그 스레드에 파일을 올리고 붙여 달라고 한다
    ② 에이전트가 draft_ops_mail 로 초안 카드를 올린다 (아직 안 나간다)
    ③ [보내기] 를 누르면 첨부를 슬랙에서 내려받아 SES 로 보낸다.
       고칠 데가 있으면 [수정]

문자와 같은 이유로 도구를 부르는 것만으로는 나가지 않습니다.

받은 메일은 service/ops_mail/inbound.py 가 슬랙에 올립니다. 스레드는 상대방 주소
하나에 하나입니다. 답장은 그 스레드에 남고, 새 메일은 카드 하나가 기록입니다 --
아직 스레드가 없던 상대가 회신하면, 스레드를 열 때 먼저 보낸 메일을 첫 메시지에
보여줍니다. 이미 스레드가 있는 상대에게 보낸 새 메일은 그 스레드에 따로 남지 않습니다.
"""

import asyncio
import dataclasses
import hashlib
import json
import os

from urllib.parse import urlparse

import aiohttp
from cachetools import TTLCache
from langchain_core.tools import StructuredTool, tool
from slack_sdk.web.async_client import AsyncWebClient

from api.ses import rejected_code, send_mime
from service.ops_mail import mime, store
from service.ops_mail.draft import (
    Draft,
    DraftProblem,
    SlackFile,
    make,
    megabytes,
    pick_files,
)
from service.slack import escape

APPROVE = "ops_mail_approve"
CANCEL = "ops_mail_cancel"
REVISE = "ops_mail_revise"
REVISE_VIEW = "ops_mail_revise_view"
FEEDBACK_BLOCK = "ops_mail_feedback"
FEEDBACK_INPUT = "ops_mail_feedback_input"

# 슬랙 버튼 value 는 2000자 제한이라 본문을 못 싣는다. 초안은 여기 둔다.
_DRAFTS: TTLCache[str, Draft] = TTLCache(maxsize=200, ttl=3600)

# 카드 section text 가 3000자라 명단과 본문을 다 싣지 못한다. 앞부분만 보이고,
# 넘치면 전체를 같은 스레드에 파일로 올린다.
PREVIEW_RECIPIENTS = 10
PREVIEW_BODY_CHARS = 1500

# 보내는 주소. IAM 정책이 From 을 이 주소로만 허용해서 설정으로 바꿀 일이 없다.
FROM_ADDRESS = "ops@team-mono.com"
FROM_HEADER = f"팀모노리스 <{FROM_ADDRESS}>"

# 몇 통마다 카드에 진행 상황을 고쳐 쓸지. chat.update 는 분당 50회 남짓이 한도다.
PROGRESS_EVERY = 20


def _preview(addresses: list[str]) -> str:
    """주소를 앞의 몇 개만 보여줍니다. 나머지는 스레드에 올린 명단 파일에 있습니다."""
    shown = ", ".join(escape(address) for address in addresses[:PREVIEW_RECIPIENTS])
    if len(addresses) > PREVIEW_RECIPIENTS:
        shown += f" 외 {len(addresses) - PREVIEW_RECIPIENTS}명"
    return shown


def _summary(draft: Draft) -> str:
    """받는 사람·제목·첨부·본문을 슬랙 텍스트로 만듭니다."""
    to = _preview(draft.recipients)
    if len(draft.recipients) > 1:
        to = f"*{len(draft.recipients)}명* — {to} · 한 명씩 따로 보냅니다"
    lines = [
        f"받는 사람: {to}",
        f"제목: {escape(draft.subject)}",
    ]
    if draft.attachments:
        names = ", ".join(
            f"{escape(file.name)} ({megabytes(file.size)})"
            for file in draft.attachments
        )
        lines.append(f"첨부: {names}")
    body = draft.body
    if len(body) > PREVIEW_BODY_CHARS:
        body = body[:PREVIEW_BODY_CHARS] + f"\n…(전체 {len(draft.body)}자는 본문 파일)"
    # 본문도 이스케이프한다. 안 하면 서명의 "<ops@team-mono.com>" 같은 글자가
    # 슬랙 문법으로 읽혀, 승인하는 사람이 실제로 나갈 본문과 다른 것을 본다.
    lines.append(f"```{escape(body).replace('`', chr(39))}```")
    return "\n".join(lines)


def _full_text_files(draft: Draft) -> list[dict]:
    """카드에 다 못 싣는 명단과 본문을 파일로 만듭니다."""
    files = []
    count = len(draft.recipients)
    if count > PREVIEW_RECIPIENTS:
        files.append(
            {
                "content": "\n".join(draft.recipients),
                "filename": f"받는사람_{count}명.txt",
                "title": f"받는 사람 {count}명",
            }
        )
    if len(draft.body) > PREVIEW_BODY_CHARS:
        files.append({"content": draft.body, "filename": "본문.txt", "title": "본문"})
    return files


def _blocks(draft_id: str, draft: Draft) -> list[dict]:
    """승인 카드를 만듭니다."""
    kind = "답장" if draft.is_reply else "새 메일"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*메일 발송 확인* — {kind}\n{_summary(draft)}",
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


def get_ops_mail_tools(
    client: AsyncWebClient, channel: str, thread_ts: str
) -> list[StructuredTool]:
    """메일 채널이면 메일 초안 도구를 돌려줍니다. 다른 채널이면 빈 목록입니다.

    회사 이름으로 나가는 메일이라 메일 채널에서만 줍니다. 모든 답변이 이 함수를
    지나므로 환경 변수가 빠져도 터지지 않게 get 으로 읽습니다.

    Args:
        client: 슬랙 클라이언트
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프

    Returns:
        list[StructuredTool]: [초안 도구] 또는 []
    """
    if channel != os.environ.get("OPS_MAIL_SLACK_CHANNEL_ID"):
        return []
    return [get_draft_ops_mail_tool(client, channel, thread_ts)]


def get_draft_ops_mail_tool(
    client: AsyncWebClient, channel: str, thread_ts: str
) -> StructuredTool:
    """ops@ 메일 발송 초안 도구를 반환합니다. 메일 채널에서만 줍니다.

    Args:
        client: 슬랙 클라이언트
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프

    Returns:
        StructuredTool: 초안 도구
    """

    @tool
    async def draft_ops_mail(
        body: str,
        to: list[str] | None = None,
        subject: str = "",
        attachments: list[str] | None = None,
    ) -> str:
        """
        ops@team-mono.com 이름으로 보낼 메일 초안을 스레드에 올립니다.
        이 도구는 메일을 보내지 않습니다. 사람이 카드의 [보내기] 를 눌러야 나갑니다.

        상대방 메일 스레드 안에서 부르면 그 상대에게 보내는 답장이 됩니다.
        받는 사람과 제목은 스레드에서 정해지므로 body 만 주면 됩니다.

        스레드 밖에서 부르면 새 메일입니다. to(받는 사람 주소 목록)와
        subject(제목)가 필요합니다. 받는 사람이 여럿이면 한 명씩 따로 보내서
        서로의 주소가 보이지 않습니다. 한 번에 500명까지입니다.

        body 는 메일 본문 텍스트입니다. 인사와 서명까지 완성된 문장으로 씁니다.

        attachments 는 붙일 파일 이름입니다. 이 스레드에 올라온 파일만 붙일 수
        있습니다. 이름이 틀리면 스레드에 있는 파일 목록을 돌려주니, 그중에서
        골라 다시 부릅니다. 첨부가 없으면 비웁니다.

        Returns:
            초안을 올렸다는 안내. 발송 결과가 아닙니다.
        """
        target = await asyncio.to_thread(store.reply_target, channel, thread_ts)
        try:
            draft = make(
                thread_ts=thread_ts,
                target=target,
                body=body,
                to=to or [],
                subject=subject,
            )
            if attachments:
                replies = await client.conversations_replies(
                    channel=channel, ts=thread_ts
                )
                draft = dataclasses.replace(
                    draft, attachments=pick_files(replies["messages"], attachments)
                )
        except DraftProblem as problem:
            return str(problem)

        draft_id = hashlib.sha256(
            json.dumps(
                [channel, dataclasses.asdict(draft)], ensure_ascii=False
            ).encode()
        ).hexdigest()[:32]
        if draft_id in _DRAFTS:
            return "같은 초안이 이미 이 스레드에 올라가 있습니다. 그 카드에서 [보내기] 를 눌러주세요."

        # 검사와 저장 사이에 await 가 있으면 한 턴에 실린 두 호출이 둘 다
        # 빠져나간다. 자리를 먼저 잡고, 카드가 못 올라가면 되돌린다.
        _DRAFTS[draft_id] = draft
        count = len(draft.recipients)
        try:
            files = _full_text_files(draft)
            if files:
                await client.files_upload_v2(
                    channel=channel, thread_ts=thread_ts, file_uploads=files
                )
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"메일 발송 확인 — {count}명",
                blocks=_blocks(draft_id, draft),
            )
        except BaseException:
            _DRAFTS.pop(draft_id, None)
            raise
        return f"{count}명에게 보낼 초안을 올렸습니다. [보내기] 를 눌러주세요."

    return draft_ops_mail


async def _download(token: str, file: SlackFile) -> mime.Attachment:
    """슬랙에 올라온 파일을 내려받습니다. 봇 토큰에 files:read 가 있어야 합니다."""
    # 봇 토큰을 싣고 가니 슬랙 파일 서버가 아니면 가지 않는다.
    if urlparse(file.url).hostname != "files.slack.com":
        raise ValueError(f"슬랙 파일이 아닙니다: {file.name}")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            file.url, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            response.raise_for_status()
            data = await response.read()
    return mime.Attachment(filename=file.name, content_type=file.mimetype, data=data)


class _Stopped(Exception):
    """한 사람에게 보내다 멈춘 이유. 카드에 남길 문구를 담고, 원래 오류를 __cause__ 로 둡니다."""

    def __init__(self, problem: str):
        super().__init__(problem)
        self.problem = problem


async def _send_one(
    draft: Draft,
    recipient: str,
    attachments: list[mime.Attachment],
    channel: str,
    user: str,
) -> None:
    """한 사람에게 보내고 남깁니다. 단계마다 무엇이 확실한지가 달라 따로 잡습니다.

    Raises:
        _Stopped: 원문 만들기(안 나감), 발송(SES 가 거절했으면 안 나감, 그 밖에는
            나갔는지 모름), 기록(나감) 중 하나가 실패
    """
    who = escape(recipient)
    try:
        message_id, raw = await asyncio.to_thread(_build, draft, recipient, attachments)
    except Exception as error:
        raise _Stopped(
            f"{who} 에게는 안 나갔습니다. ({type(error).__name__})"
        ) from error

    try:
        ses_id = (await asyncio.to_thread(send_mime, raw))["MessageId"]
    except Exception as error:
        if code := rejected_code(error):
            raise _Stopped(f"{who} 에게는 안 나갔습니다 ({code}).") from error
        # 타임아웃·5xx 는 SES 가 이미 접수했을 수 있다. "실패" 라고 하면 다시 보내고,
        # 같은 사람이 두 번 받는다.
        raise _Stopped(
            f"{who} 는 발송 여부를 모릅니다 — 받는 쪽에 확인하세요."
            f" ({type(error).__name__})"
        ) from error
    print(f"OPS MAIL SENT {channel} {user} {recipient} {ses_id}")

    # 나가자마자 남긴다. 다 보낸 뒤에 몰아서 남기면, 그 사이에 멈췄을 때 나간 메일의
    # 기록이 빠진다.
    try:
        await _record(draft, recipient, channel, message_id, ses_id, user)
    except Exception as error:
        raise _Stopped(
            f"{who} 에게는 보냈지만 기록하지 못했습니다. ({type(error).__name__})"
        ) from error


def _build(
    draft: Draft, recipient: str, attachments: list[mime.Attachment]
) -> tuple[str, bytes]:
    """한 사람에게 보낼 원문을 만듭니다.

    Returns:
        tuple[str, bytes]: (우리가 만든 Message-ID, MIME 원문)
    """
    return mime.build(
        from_header=FROM_HEADER,
        to_address=recipient,
        subject=draft.subject,
        body=draft.body,
        references_ids=draft.references_ids,
        attachments=attachments,
    )


async def _record(
    draft: Draft,
    recipient: str,
    channel: str,
    message_id: str,
    ses_id: str,
    user: str,
) -> None:
    """보낸 메일 한 통을 남깁니다."""
    await asyncio.to_thread(
        store.insert,
        store.OpsMailRow(
            direction="out",
            message_id=message_id,
            ses_message_id=ses_id,
            counterpart=recipient,
            from_address=FROM_ADDRESS,
            to_addresses=[recipient],
            subject=draft.subject,
            body_text=draft.body,
            references_ids=draft.references_ids,
            channel_id=channel,
            sent_by=user,
        ),
    )


def _revise_view(draft_id: str, channel: str, ts: str, draft: Draft) -> dict:
    """수정 요청 모달입니다. 직접 고치게 하지 않고 피드백만 받습니다."""
    return {
        "type": "modal",
        "callback_id": REVISE_VIEW,
        "private_metadata": json.dumps(
            {"draft_id": draft_id, "channel": channel, "ts": ts}
        ),
        "title": {"type": "plain_text", "text": "메일 수정 요청"},
        "submit": {"type": "plain_text", "text": "다시 쓰기"},
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": _summary(draft)}},
            {
                "type": "input",
                "block_id": FEEDBACK_BLOCK,
                "label": {"type": "plain_text", "text": "어디를 어떻게 고칠까요?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": FEEDBACK_INPUT,
                    "multiline": True,
                },
            },
        ],
    }


def register_ops_mail_handlers(app, revise=None):
    """승인·수정·취소 버튼 핸들러를 등록합니다.

    Args:
        app: slack_bolt AsyncApp
        revise: 수정 피드백을 초안을 쓴 에이전트에게 되돌리는 콜백.
            `async (channel, thread_ts, user, text) -> None` 입니다.
    """

    @app.action(APPROVE)
    async def approve(ack, body, client):
        await ack()
        draft_id = body["actions"][0]["value"]
        # 꺼내면서 지운다. 두 번 눌러도 두 번 보내지 않는다.
        draft = _DRAFTS.pop(draft_id, None)
        if draft is None:
            await _gone(client, body)
            return
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]
        user = body["user"]["id"]
        total = len(draft.recipients)

        # 버튼부터 걷는다. 첨부를 받고 수백 명에게 보내는 동안 다른 사람이 [취소] 를
        # 눌러도 아무 일도 안 일어나는데, 누른 사람은 막았다고 믿는다. 못 걷었으면
        # 아직 아무것도 안 했으니 초안을 되돌려 다시 누를 수 있게 한다.
        try:
            await _replace(client, channel, ts, _sending(0, total))
        except BaseException:
            _DRAFTS[draft_id] = draft
            raise

        # 내려받기는 보내기 전이라 실패해도 메일은 나가지 않았다.
        try:
            attachments = [
                await _download(client.token, file) for file in draft.attachments
            ]
        except Exception as error:
            await _note(
                client,
                channel,
                ts,
                f"첨부파일을 받지 못해 보내지 않았습니다 ({type(error).__name__}) — 초안을 다시 만들어주세요",
            )
            raise

        # 한 명씩 따로 보낸다. 받는 사람끼리 서로의 주소가 보이지 않고, 회신은
        # 각자의 상대방 스레드로 들어온다.
        for sent, recipient in enumerate(draft.recipients):
            # 무엇이 실패하든 나머지는 보내지 않는다. 같은 원인으로 줄줄이 실패하거나
            # 같은 상태를 늘린다. 카드에는 어디서 멈췄는지 남긴다.
            try:
                await _send_one(draft, recipient, attachments, channel, user)
            except _Stopped as stopped:
                await _stop(client, channel, ts, draft, sent, stopped.problem)
                raise stopped.__cause__
            if (sent + 1) % PROGRESS_EVERY == 0 and sent + 1 < total:
                await _note(client, channel, ts, _sending(sent + 1, total))

        await _note(
            client,
            channel,
            ts,
            f"📤 <@{user}> 님이 {total}명에게 보냈습니다\n{_summary(draft)}",
        )

    @app.action(REVISE)
    async def open_revise(ack, body, client):
        await ack()
        draft_id = body["actions"][0]["value"]
        draft = _DRAFTS.get(draft_id)
        # 여기서는 꺼내지 않는다. 모달을 닫아 버리면 멀쩡한 카드의 [보내기] 가 죽는다.
        if draft is None:
            await _gone(client, body)
            return
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=_revise_view(
                draft_id,
                body["container"]["channel_id"],
                body["container"]["message_ts"],
                draft,
            ),
        )

    @app.view(REVISE_VIEW)
    async def submit_revise(ack, body, client):
        await ack()
        meta = json.loads(body["view"]["private_metadata"])
        feedback = body["view"]["state"]["values"][FEEDBACK_BLOCK][FEEDBACK_INPUT][
            "value"
        ]
        # 이 카드의 [보내기] 는 이제 옛 문안이라 눌리면 안 된다.
        draft = _DRAFTS.pop(meta["draft_id"], None)
        if draft is None:
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
        await revise(
            channel=meta["channel"],
            thread_ts=draft.thread_ts,
            user=user,
            text=feedback,
        )

    @app.action(CANCEL)
    async def cancel(ack, body, client):
        await ack()
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        if draft is None:
            await _gone(client, body)
            return
        await _replace(
            client,
            body["container"]["channel_id"],
            body["container"]["message_ts"],
            f"<@{body['user']['id']}> 님이 취소했습니다.",
        )


async def _stop(
    client, channel: str, ts: str, draft: Draft, sent: int, problem: str
) -> None:
    """발송을 멈추고 카드에 어디까지 나갔는지 남깁니다.

    문제가 생긴 사람은 recipients[sent] 이고, 그다음 사람부터 보내지 않았습니다.

    Args:
        client: 슬랙 클라이언트
        channel: 카드가 있는 채널
        ts: 카드 ts
        draft: 초안
        sent: 문제가 생긴 사람 앞까지 나간 통 수
        problem: 문제가 생긴 사람에게 무슨 일이 있었는지
    """
    left = draft.recipients[sent + 1 :]
    await _note(
        client,
        channel,
        ts,
        f"⚠️ {sent}명에게 보냈습니다. {problem}"
        + (f"\n보내지 않은 {len(left)}명: {_preview(left)}" if left else "")
        + f"\n{_summary(draft)}",
    )
    # 카드에는 앞의 몇 명만 보인다. 남은 사람에게 다시 보내려면 전체 명단이 있어야
    # 하는데, 처음 올린 명단 파일에서 손으로 잘라내면 이미 받은 사람이 섞인다.
    if len(left) > PREVIEW_RECIPIENTS:
        try:
            await client.files_upload_v2(
                channel=channel,
                thread_ts=draft.thread_ts,
                content="\n".join(left),
                filename=f"보내지않은사람_{len(left)}명.txt",
                title=f"보내지 않은 {len(left)}명",
            )
        except Exception as error:
            print(
                f"ops 메일 남은 명단 올리기 실패 {channel} {ts} {type(error).__name__}"
            )


def _sending(sent: int, total: int) -> str:
    """보내는 중인 카드 문구입니다.

    봇이 보내다 재시작되면 카드가 이 문구에서 멈춘다. 숫자는 20통마다 고치므로
    실제로는 더 나갔을 수 있다. 그대로 다시 보내면 이미 받은 사람이 또 받는다.
    """
    return (
        f"📤 보내는 중… {sent}/{total}\n"
        "_오래 멈춰 있으면 봇이 재시작된 것입니다. 이미 받은 사람이 있으니"
        " 전체를 다시 보내지 말고 개발팀에 물어보세요._"
    )


async def _note(client, channel: str, ts: str, text: str) -> None:
    """발송을 시작한 뒤 카드에 진행 상황이나 결과를 고쳐 씁니다. 실패해도 던지지 않습니다.

    여기서 던지면 발송 루프가 남은 사람을 아무도 모르게 건너뛰거나, 발송이 멈춘
    원래 이유가 이 오류에 가려진다. 이때는 로그가 기록이다.
    """
    try:
        await _replace(client, channel, ts, text)
    except Exception as error:
        print(
            f"ops 메일 카드 고치기 실패 {channel} {ts} {type(error).__name__}: {text}"
        )


async def _gone(client, body: dict) -> None:
    """만료됐거나 이미 처리된 카드의 버튼을 누른 사람에게만 알립니다.

    카드는 고치지 않는다. 다른 사람이 막 [보내기] 를 눌러 보내는 중일 수 있다.
    """
    try:
        await client.chat_postEphemeral(
            channel=body["container"]["channel_id"],
            user=body["user"]["id"],
            text="이 메일 카드는 이미 처리됐거나 만료됐습니다(봇 재시작·1시간 경과)."
            " 카드에 보이는 상태를 확인하고, 필요하면 초안을 다시 만들어주세요.",
        )
    except Exception as error:
        print(f"ops 메일 만료 안내 실패 {type(error).__name__}")


async def _replace(client, channel: str, ts: str, text: str) -> None:
    """카드를 결과 문구로 바꿉니다. 버튼이 남아 있으면 또 눌린다."""
    await client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
