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

받은 메일은 app/ops_mail_sns.py 가 슬랙에 올립니다. 스레드는 상대방 주소 하나에
하나입니다. 답장은 그 스레드에 남고, 새 메일은 카드 하나가 기록입니다 -- 상대가
회신해서 스레드가 열릴 때 먼저 보낸 메일이 그 스레드에 함께 보입니다.
"""

import asyncio
import hashlib
import json

import aiohttp
from cachetools import TTLCache
from langchain_core.tools import StructuredTool, tool
from slack_sdk.web.async_client import AsyncWebClient

from api.ses import send_raw_email
from service.db import connect
from service.ops_mail import mime, store

APPROVE = "ops_mail_approve"
CANCEL = "ops_mail_cancel"
REVISE = "ops_mail_revise"
REVISE_VIEW = "ops_mail_revise_view"
FEEDBACK_BLOCK = "ops_mail_feedback"
FEEDBACK_INPUT = "ops_mail_feedback_input"

# 슬랙 버튼 value 는 2000자 제한이라 본문을 못 싣는다. 초안은 여기 둔다.
_DRAFTS: TTLCache = TTLCache(maxsize=200, ttl=3600)

# 첨부 크기 합의 상한. SES v2 는 한 통에 40MB 까지인데 base64 인코딩 뒤
# 크기로 잰다. 인코딩하면 4/3 배가 되니 원본으로는 30MB 남짓이고, 본문과
# 헤더 자리를 남겨 28MB 로 둔다.
MAX_ATTACHMENT_BYTES = 28 * 1024 * 1024

# 한 번에 보내는 받는 사람 수의 상한. 명단을 잘못 넣어 수천 명에게 나가는 것을 막는다.
MAX_RECIPIENTS = 500

# 카드에 보여주는 주소 수. 카드 section text 가 3000자라 전부는 못 싣는다.
# 넘으면 전체 명단을 같은 스레드에 파일로 올린다.
PREVIEW_RECIPIENTS = 10

# 보내는 주소. IAM 정책이 From 을 이 주소로만 허용해서 설정으로 바꿀 일이 없다.
FROM_ADDRESS = "ops@team-mono.com"
FROM_HEADER = f"팀모노리스 <{FROM_ADDRESS}>"

# 몇 통마다 카드에 진행 상황을 고쳐 쓸지. chat.update 는 분당 50회 남짓이 한도다.
PROGRESS_EVERY = 20


def escape(text: str) -> str:
    """슬랙 mrkdwn 에서 특수문자로 읽히는 것만 바꿉니다."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_store(function, *args):
    """store 함수 하나를 새 커넥션으로 부릅니다. psycopg 는 동기라 스레드로 넘겨 씁니다."""
    with connect() as conn:
        return function(conn, *args)


def _megabytes(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


def _preview(addresses: list[str]) -> str:
    """주소를 앞의 몇 개만 보여줍니다. 나머지는 스레드에 올린 명단 파일에 있습니다."""
    shown = ", ".join(escape(address) for address in addresses[:PREVIEW_RECIPIENTS])
    if len(addresses) > PREVIEW_RECIPIENTS:
        shown += f" 외 {len(addresses) - PREVIEW_RECIPIENTS}명"
    return shown


def _summary(draft: dict) -> str:
    """받는 사람·제목·첨부·본문을 슬랙 텍스트로 만듭니다."""
    recipients = draft["recipients"]
    to = _preview(recipients)
    if len(recipients) > 1:
        to = f"*{len(recipients)}명* — {to} · 한 명씩 따로 보냅니다"
    lines = [
        f"받는 사람: {to}",
        f"제목: {escape(draft['subject'])}",
    ]
    if draft["attachments"]:
        names = ", ".join(
            f"{escape(file['name'])} ({_megabytes(file['size'])})"
            for file in draft["attachments"]
        )
        lines.append(f"첨부: {names}")
    lines.append(f"```{draft['body'].replace('`', chr(39))}```")
    return "\n".join(lines)


def _pick_files(messages: list[dict], names: list[str]) -> list[dict] | str:
    """스레드에 올라온 파일 중 붙일 것을 이름으로 고릅니다.

    에이전트가 파일을 지어내지 못하게 스레드에 실제로 있는 파일만 받습니다.
    같은 이름이 여러 번 올라왔으면 나중에 올린 것을 씁니다.

    Args:
        messages: conversations_replies 의 메시지들
        names: 붙일 파일 이름

    Returns:
        list[dict] | str: 고른 파일. 없는 이름이 있거나 너무 크면 고칠 것을 적은 문자열
    """
    files = {}
    for message in messages:
        for file in message.get("files", []):
            files[file["name"]] = file

    missing = [name for name in names if name not in files]
    if missing:
        return (
            f"스레드에 {', '.join(missing)} 파일이 없습니다. "
            f"스레드에 올라온 파일: {', '.join(files) or '없음'}"
        )

    picked = [files[name] for name in dict.fromkeys(names)]
    total = sum(file["size"] for file in picked)
    if total > MAX_ATTACHMENT_BYTES:
        return (
            f"첨부가 합쳐서 {_megabytes(total)} 라 보낼 수 없습니다. "
            f"{_megabytes(MAX_ATTACHMENT_BYTES)} 까지입니다."
        )
    return [
        {
            "name": file["name"],
            "mimetype": file["mimetype"],
            "size": file["size"],
            "url": file["url_private_download"],
        }
        for file in picked
    ]


def _blocks(draft_id: str, draft: dict) -> list[dict]:
    """승인 카드를 만듭니다."""
    kind = "답장" if draft["references_ids"] else "새 메일"
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


def _reply_references(latest: dict) -> list[str]:
    """답장의 References 를 만듭니다. 마지막 것이 In-Reply-To 가 됩니다.

    SES 는 보낼 때 Message-ID 를 자기 값으로 바꿔 씁니다. 그래서 우리가 보낸 메일의
    message_id 는 상대가 본 적 없는 값이라 넣지 않습니다. 우리가 보낸 메일의
    references_ids 에는 그때까지 상대 메일의 ID 가 이미 들어 있습니다.
    """
    if latest["direction"] == "out":
        return latest["references_ids"]
    return [*latest["references_ids"], latest["message_id"]]


def _prepare(
    channel: str, thread_ts: str, body: str, to: list[str], subject: str
) -> dict | str:
    """초안을 만듭니다. 상대방 스레드 안이면 답장, 밖이면 새 메일입니다.

    Args:
        channel: 채널 ID
        thread_ts: 도구를 부른 스레드
        body: 본문
        to: 받는 사람 주소들. 답장이면 비워도 됩니다
        subject: 제목. 답장이면 비워도 됩니다

    Returns:
        dict | str: 초안. 보낼 수 없으면 고칠 것을 적은 문자열
    """
    recipients = list(
        dict.fromkeys(address.strip().lower() for address in to if address.strip())
    )
    latest = run_store(store.latest_in_thread, channel, thread_ts)

    if latest:
        if recipients and recipients != [latest["counterpart"]]:
            return (
                f"이 스레드는 {latest['counterpart']} 와의 메일 스레드라 받는 사람을"
                " 바꿀 수 없습니다. 다른 사람에게는 채널에서 새 메일로 보내주세요."
            )
        return {
            "recipients": [latest["counterpart"]],
            "subject": mime.reply_subject(latest["subject"]),
            "body": body,
            "references_ids": _reply_references(latest),
        }

    if not recipients or not subject:
        return "새 메일은 받는 사람(to)과 제목(subject)이 필요합니다."
    invalid = [address for address in recipients if "@" not in address]
    if invalid:
        return f"메일 주소가 아닙니다: {', '.join(invalid)}"
    if len(recipients) > MAX_RECIPIENTS:
        return (
            f"받는 사람이 {len(recipients)}명입니다. 한 번에 {MAX_RECIPIENTS}명까지라"
            " 나눠서 올려주세요."
        )
    return {
        "recipients": recipients,
        "subject": subject,
        "body": body,
        "references_ids": [],
    }


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
        draft = await asyncio.to_thread(
            _prepare, channel, thread_ts, body, to or [], subject
        )
        if isinstance(draft, str):
            return draft

        files = []
        if attachments:
            replies = await client.conversations_replies(channel=channel, ts=thread_ts)
            files = _pick_files(replies["messages"], attachments)
            if isinstance(files, str):
                return files
        draft = {**draft, "attachments": files}

        draft_id = hashlib.sha256(
            json.dumps([channel, thread_ts, draft], ensure_ascii=False).encode()
        ).hexdigest()[:32]
        if draft_id in _DRAFTS:
            return "같은 초안이 이미 이 스레드에 올라가 있습니다. 그 카드에서 [보내기] 를 눌러주세요."

        # 검사와 저장 사이에 await 가 있으면 한 턴에 실린 두 호출이 둘 다
        # 빠져나간다. 자리를 먼저 잡고, 카드가 못 올라가면 되돌린다.
        _DRAFTS[draft_id] = {**draft, "thread_ts": thread_ts}
        count = len(draft["recipients"])
        try:
            if count > PREVIEW_RECIPIENTS:
                await client.files_upload_v2(
                    channel=channel,
                    thread_ts=thread_ts,
                    content="\n".join(draft["recipients"]),
                    filename=f"받는사람_{count}명.txt",
                    title=f"받는 사람 {count}명",
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


async def _download(token: str, file: dict) -> mime.Attachment:
    """슬랙에 올라온 파일을 내려받습니다. 봇 토큰에 files:read 가 있어야 합니다."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            file["url"], headers={"Authorization": f"Bearer {token}"}
        ) as response:
            response.raise_for_status()
            data = await response.read()
    return mime.Attachment(
        filename=file["name"], content_type=file["mimetype"], data=data
    )


def _send(
    draft: dict, recipient: str, attachments: list[mime.Attachment]
) -> tuple[str, str]:
    """한 사람에게 SES 로 보냅니다.

    Returns:
        tuple[str, str]: (우리가 만든 Message-ID, SES MessageId)
    """
    message_id, raw = mime.build(
        from_header=FROM_HEADER,
        to_address=recipient,
        subject=draft["subject"],
        body=draft["body"],
        references_ids=draft["references_ids"],
        attachments=attachments,
    )
    response = send_raw_email(raw)
    return message_id, response["MessageId"]


def _record(
    draft: dict,
    recipient: str,
    channel: str,
    thread_ts: str | None,
    message_id: str,
    ses_id: str,
    user: str,
) -> None:
    """보낸 메일 한 통을 남깁니다."""
    run_store(
        store.insert,
        store.OpsMailRow(
            direction="out",
            message_id=message_id,
            ses_message_id=ses_id,
            counterpart=recipient,
            from_address=FROM_ADDRESS,
            to_addresses=[recipient],
            subject=draft["subject"],
            body_text=draft["body"],
            references_ids=draft["references_ids"],
            s3_key=None,
            channel_id=channel,
            thread_ts=thread_ts,
            sent_by=user,
        ),
    )


def _revise_view(draft_id: str, channel: str, ts: str, draft: dict) -> dict:
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
        # 꺼내면서 지운다. 두 번 눌러도 두 번 보내지 않는다.
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        if draft is None:
            return
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]
        user = body["user"]["id"]

        # 내려받기는 보내기 전이라 실패해도 메일은 나가지 않았다.
        try:
            attachments = [
                await _download(client.token, file) for file in draft["attachments"]
            ]
        except Exception as error:
            await _replace(
                client,
                channel,
                ts,
                f"첨부파일을 받지 못해 보내지 않았습니다 ({type(error).__name__}) — 초안을 다시 만들어주세요",
            )
            raise

        # 한 명씩 따로 보낸다. 받는 사람끼리 서로의 주소가 보이지 않고, 회신은
        # 각자의 상대방 스레드로 들어온다. 답장은 카드가 있는 스레드가 곧 상대방
        # 스레드이고, 새 메일은 회신이 와서 스레드가 열릴 때 묶인다.
        thread_ts = draft["thread_ts"] if draft["references_ids"] else None
        total = len(draft["recipients"])
        sent = []
        # 버튼을 먼저 걷는다. 수백 명이면 다 보내는 데 1분 남짓 걸린다.
        await _replace(client, channel, ts, f"📤 보내는 중… 0/{total}")
        for recipient in draft["recipients"]:
            # 무엇이 실패하든 나머지는 보내지 않는다. 같은 원인으로 줄줄이 실패하거나
            # 같은 상태를 늘린다. 카드에는 어디서 멈췄는지 남긴다.
            try:
                message_id, ses_id = await asyncio.to_thread(
                    _send, draft, recipient, attachments
                )
            except Exception as error:
                # 타임아웃·5xx 는 SES 가 이미 접수했을 수 있다. "실패" 라고 하면
                # 다시 보내고, 같은 사람이 두 번 받는다.
                await _stop(
                    client,
                    channel,
                    ts,
                    draft,
                    len(sent),
                    f"{recipient} 는 발송 여부를 모릅니다 ({type(error).__name__})"
                    " — 받는 쪽에 확인하세요.",
                )
                raise
            print(f"OPS MAIL SENT {channel} {user} {recipient} {ses_id}")
            # 나가자마자 남긴다. 다 보낸 뒤에 몰아서 남기면, 그 사이에 멈췄을 때
            # 나간 메일의 기록이 빠진다.
            try:
                await asyncio.to_thread(
                    _record,
                    draft,
                    recipient,
                    channel,
                    thread_ts,
                    message_id,
                    ses_id,
                    user,
                )
            except Exception as error:
                await _stop(
                    client,
                    channel,
                    ts,
                    draft,
                    len(sent),
                    f"{recipient} 에게는 보냈지만 기록하지 못했습니다"
                    f" ({type(error).__name__}).",
                )
                raise
            sent.append(recipient)
            if len(sent) % PROGRESS_EVERY == 0 and len(sent) < total:
                await _replace(
                    client, channel, ts, f"📤 보내는 중… {len(sent)}/{total}"
                )

        await _replace(
            client,
            channel,
            ts,
            f"📤 <@{user}> 님이 {len(sent)}명에게 보냈습니다\n{_summary(draft)}",
        )

    @app.action(REVISE)
    async def open_revise(ack, body, client):
        await ack()
        draft_id = body["actions"][0]["value"]
        draft = _DRAFTS.get(draft_id)
        # 여기서는 꺼내지 않는다. 모달을 닫아 버리면 멀쩡한 카드의 [보내기] 가 죽는다.
        if draft is None:
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
            thread_ts=draft["thread_ts"],
            user=user,
            text=feedback,
        )

    @app.action(CANCEL)
    async def cancel(ack, body, client):
        await ack()
        draft = _DRAFTS.pop(body["actions"][0]["value"], None)
        if draft is None:
            return
        await _replace(
            client,
            body["container"]["channel_id"],
            body["container"]["message_ts"],
            f"<@{body['user']['id']}> 님이 취소했습니다.",
        )


async def _stop(
    client, channel: str, ts: str, draft: dict, sent: int, problem: str
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
    left = draft["recipients"][sent + 1 :]
    await _replace(
        client,
        channel,
        ts,
        f"⚠️ {sent}명에게 보냈습니다. {problem}"
        + (f"\n보내지 않은 {len(left)}명: {_preview(left)}" if left else "")
        + f"\n{_summary(draft)}",
    )


async def _replace(client, channel: str, ts: str, text: str) -> None:
    """카드를 결과 문구로 바꿉니다. 버튼이 남아 있으면 또 눌린다."""
    await client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
