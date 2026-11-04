"""
S3 에 저장된 받은 메일을 슬랙에 올리고 남깁니다.

웹훅(app/ops_mail_sns.py)과 정리 작업(scripts/reconcile_ops_mail.py)이 같은 함수를
부릅니다. 둘은 다른 프로세스라, 같은 원본을 동시에 맡지 않게 하는 것은 DB 입니다.

슬랙 스레드는 상대방 주소 하나에 하나입니다. 처음 오가는 상대면 채널에
상대 주소로 스레드를 열고, 메일은 늘 그 스레드의 댓글로 답니다. 우리가 새 메일로
먼저 보낸 상대라면 그 메일들을 스레드 첫 메시지에 보여줍니다.

자동응답(부재중 등)은 이미 있는 스레드에만 달고 새 스레드를 열지 않습니다.
여러 명에게 보낸 메일에 돌아오는 자동응답이 채널을 덮지 않게 하려는 것입니다.
답장할 메일을 고를 때도 자동응답은 건너뜁니다. 메일링 리스트·대량 발송 메일은
사람이 보낸 메일처럼 다룹니다.

처리는 S3 객체 키 하나로 끝납니다. 검사 결과는 SES 가 원문 헤더에 붙이고,
객체 키의 끝이 SES 메시지 ID 라 SNS 알림 본문이 따로 필요 없습니다.
"""

import asyncio
import os
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from api.s3 import get_object
from service.ops_mail import mime, store
from service.slack import escape

KST = ZoneInfo("Asia/Seoul")

# 슬랙 메시지 한 개에 싣는 본문 길이. 넘으면 자르고 원본 위치를 남긴다.
MAX_BODY_CHARS = 3500

# 원본 하나를 처리하며 기다리는 시간의 상한. 자리를 잡아 두는 시간(store.LEASE)보다
# 짧아야 한다 -- 길면 아직 처리 중인 원본을 다른 쪽이 다시 맡아 두 번 올린다.
# 넘으면 슬랙·DB 다음 단계로 넘어가지 않게 끊을 뿐, 이미 스레드로 넘긴 S3 읽기나
# 파싱, DB 호출은 끝까지 돈다. 그 시간을 남겨 LEASE 보다 5분 짧게 둔다.
DELIVER_TIMEOUT = store.LEASE - timedelta(minutes=5)


def format_inbound(mail: mime.ParsedMail, s3_uri: str) -> str:
    """받은 메일을 슬랙 메시지로 만듭니다.

    Args:
        mail: 읽은 메일
        s3_uri: 원본 위치

    Returns:
        str: 슬랙 메시지 텍스트
    """
    warnings = []
    if mail.spam:
        warnings.append("⚠️ 스팸 의심")
    if mail.virus:
        warnings.append("⚠️ 바이러스 의심 — 첨부파일을 올리지 않았습니다")
    # 스레드는 회신 주소로 고른다. 회신 주소에 남의 주소를 적으면 그 사람 스레드에
    # 들어오니, 보낸 사람과 다르면 드러낸다.
    if mail.reply_to_address != mail.from_address:
        warnings.append(
            f"⚠️ 회신 주소가 보낸 사람과 다릅니다 — 답장은"
            f" {escape(mail.reply_to_address)} 로 갑니다"
        )

    body = mail.body_text
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n…(이하 생략, 원본 {s3_uri})"

    lines = [
        f"📨 *{escape(mail.subject or '(제목 없음)')}*",
        f"보낸 사람: {escape(mail.from_header)}",
        f"받는 사람: {escape(', '.join(mail.to_addresses))}",
        *warnings,
        "",
        escape(body),
    ]
    return "\n".join(lines)


def format_thread_root(counterpart: str, sent: list[dict[str, Any]]) -> str:
    """상대방 스레드의 첫 메시지를 만듭니다.

    Args:
        counterpart: 상대방 주소
        sent: 스레드가 열리기 전에 이 상대에게 보낸 메일

    Returns:
        str: 슬랙 메시지 텍스트
    """
    lines = [f"✉️ {escape(counterpart)} 와 주고받은 메일"]
    lines += [
        f"📤 먼저 보낸 메일: {escape(mail['subject'])}"
        f" ({mail['created_at'].astimezone(KST):%m/%d %H:%M}, <@{mail['sent_by']}> 님)"
        for mail in sent
    ]
    return "\n".join(lines)


def format_given_up(row: dict[str, Any], bucket: str) -> str:
    """더 다시 처리하지 않는 원본을 알리는 메시지입니다.

    Args:
        row: store.given_up 한 행 (s3_key, last_error)
        bucket: 메일 원본 버킷
    """
    # 이유가 없으면 마지막 처리 중에 프로세스가 죽은 것이다.
    reason = row["last_error"] or "처리 중에 서버가 멈춤"
    return (
        f"⚠️ 받은 메일을 {store.MAX_ATTEMPTS}번 처리하지 못해 더 다시 하지 않습니다\n"
        f"이유: `{escape(reason).replace('`', chr(39))}`\n"
        f"원본: s3://{bucket}/{row['s3_key']}"
    )


async def _open_thread(client: AsyncWebClient, channel: str, counterpart: str) -> str:
    """상대방 스레드를 열고 등록합니다.

    같은 상대의 메일 두 통이 동시에 들어오면(웹훅과 정리 작업은 다른 프로세스다)
    둘 다 스레드를 연다. 등록은 하나만 되니, 진 쪽은 자기가 연 것을 지우고
    이긴 스레드를 씁니다.
    """
    sent = await asyncio.to_thread(store.sent_to, channel, counterpart)
    response = await client.chat_postMessage(
        channel=channel, text=format_thread_root(counterpart, sent)
    )
    thread_ts = await asyncio.to_thread(
        store.claim_thread, channel, counterpart, response["ts"]
    )
    if thread_ts != response["ts"]:
        await client.chat_delete(channel=channel, ts=response["ts"])
    return thread_ts


def ses_message_id(key: str) -> str:
    """S3 객체 키의 끝이 SES 메시지 ID 입니다. 수신함은 이 값으로 원본을 가립니다."""
    return key.rsplit("/", 1)[-1]


async def process_object(bucket: str, key: str) -> None:
    """S3 에 저장된 받은 메일 하나를 슬랙에 올리고 남깁니다.

    원본을 읽기 전에 자리를 잡습니다. SNS 가 같은 알림을 두 번 보내도, 정리 작업이
    같은 원본을 다시 집어도 동시에 둘이 올리지 않습니다. 실패하면 정리 작업이 다시
    맡습니다 -- 메일은 올렸는데 첨부에서 실패했다면 다시 맡을 때 메일이 한 번 더
    올라갑니다. 횟수를 다 쓰면 정리 작업이 채널에 원본 위치를 알립니다.

    Args:
        bucket: 메일 원본 버킷
        key: 객체 키. 끝이 SES 메시지 ID 입니다.
    """
    ses_id = ses_message_id(key)
    if await asyncio.to_thread(store.claim, ses_id, key) is None:
        print(f"ops 메일 건너뜀(처리됐거나 처리 중) {key}")
        return
    try:
        await asyncio.wait_for(
            _deliver(bucket, key, ses_id), DELIVER_TIMEOUT.total_seconds()
        )
    except Exception as error:
        # 횟수를 다 쓰면 정리 작업이 이 이유를 채널에 알린다. 남기기마저 실패해도
        # 원래 실패를 가리지 않는다.
        reason = f"{type(error).__name__}: {error}"[:500]
        try:
            await asyncio.to_thread(store.fail, ses_id, reason)
        except Exception as store_error:
            print(f"ops 메일 실패 이유 기록 실패 {key} {type(store_error).__name__}")
        raise


async def _deliver(bucket: str, key: str, ses_id: str) -> None:
    """원본을 읽어 상대방 스레드에 올리고, 다 올리면 남깁니다."""
    raw = await asyncio.to_thread(get_object, bucket, key)
    # 첨부 디코딩과 HTML 파싱이 무거워 이벤트 루프에서 돌리지 않는다.
    mail = await asyncio.to_thread(mime.parse, raw)
    channel_id = os.environ["OPS_MAIL_SLACK_CHANNEL_ID"]
    counterpart = mail.reply_to_address

    # 보낸 사람 주소가 없는 원본(SES 가 수신 규칙을 만들 때 넣는 안내 등)은 답장할
    # 상대가 없어 슬랙에 올리지 않는다. 올리면 주소 없는 스레드 하나에 다 모인다.
    thread_ts = None
    if counterpart:
        client = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
        thread_ts = await asyncio.to_thread(store.find_thread, channel_id, counterpart)
        # 자동응답은 이미 있는 스레드에만 단다. 없으면 기록만 남긴다.
        if thread_ts is None and not mail.auto_reply:
            thread_ts = await _open_thread(client, channel_id, counterpart)
        if thread_ts:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=format_inbound(mail, f"s3://{bucket}/{key}"),
            )
            if mail.attachments and not mail.virus:
                await client.files_upload_v2(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    file_uploads=[
                        {"file": a.data, "filename": a.filename, "title": a.filename}
                        for a in mail.attachments
                    ],
                )

    await asyncio.to_thread(
        store.finish,
        store.OpsMailRow(
            direction="in",
            message_id=mail.message_id,
            ses_message_id=ses_id,
            counterpart=counterpart,
            from_address=mail.from_address,
            to_addresses=mail.to_addresses,
            subject=mail.subject,
            body_text=mail.body_text,
            references_ids=mail.references_ids,
            channel_id=channel_id,
            sent_by=None,
            auto_reply=mail.auto_reply,
            received_at=mail.received_at,
        ),
    )
    print(f"ops 메일 수신 {counterpart} {mail.message_id} → {thread_ts}")
