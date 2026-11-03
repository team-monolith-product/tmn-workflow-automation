"""
ops@team-mono.com 으로 온 메일을 받아 슬랙에 올리는 엔드포인트입니다.

    외부 → ops@team-mono.com → (구글 라우팅) → ops@in.team-mono.com
    → SES 수신 규칙 → S3 원본 저장 + SNS 알림 → 여기

SES 는 임의 URL 로 요청을 보내지 못해서 SNS 의 HTTPS 구독으로 받습니다.
구독을 만들면 SubscriptionConfirmation 이 한 번 오고, SubscribeURL 을 열어야
구독이 살아납니다.

슬랙 스레드는 상대방 주소 하나에 하나입니다. 처음 오가는 상대면 채널에
상대 주소로 스레드를 열고, 메일은 늘 그 스레드의 댓글로 답니다. 우리가 새 메일로
먼저 보낸 상대라면 그 메일들을 스레드 첫 메시지에 보여줍니다.

자동응답(부재중 등)은 이미 있는 스레드에만 달고 새 스레드를 열지 않습니다.
여러 명에게 보낸 메일에 돌아오는 자동응답이 채널을 덮지 않게 하려는 것입니다.

처리는 S3 객체 키 하나로 끝납니다. 검사 결과는 SES 가 원문 헤더에 붙이고,
객체 키의 끝이 SES 메시지 ID 라 SNS 알림 본문이 따로 필요 없습니다. 그래서
처리 중 실패한 메일은 scripts/reconcile_ops_mail.py 가 S3 를 훑어 다시 처리합니다.

응답을 먼저 돌려주고 처리는 그 뒤에 합니다. 응답이 늦으면 SNS 가 실패로 보고
같은 알림을 다시 보냅니다.
"""

import asyncio
import json
import os
from typing import Any

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from slack_sdk.web.async_client import AsyncWebClient

from api.s3 import get_object
from app.common import KST
from service.ops_mail import mime, sns, store
from service.slack import escape

router = APIRouter()

# 슬랙 메시지 한 개에 싣는 본문 길이. 넘으면 자르고 원본 위치를 남긴다.
MAX_BODY_CHARS = 3500


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


async def _open_thread(client: AsyncWebClient, channel: str, counterpart: str) -> str:
    """상대방 스레드를 열고 등록합니다.

    같은 상대의 메일 두 통이 동시에 들어오면(웹훅과 정리 작업은 다른 프로세스다)
    둘 다 스레드를 연다. 등록은 하나만 되니, 진 쪽은 자기가 연 것을 지우고
    이긴 스레드를 씁니다.
    """
    sent = await store.run(store.sent_to, channel, counterpart)
    response = await client.chat_postMessage(
        channel=channel, text=format_thread_root(counterpart, sent)
    )
    thread_ts = await store.run(
        store.claim_thread, channel, counterpart, response["ts"]
    )
    if thread_ts != response["ts"]:
        await client.chat_delete(channel=channel, ts=response["ts"])
    return thread_ts


async def process_object(bucket: str, key: str) -> None:
    """S3 에 저장된 받은 메일 하나를 슬랙에 올리고 남깁니다.

    Args:
        bucket: 메일 원본 버킷
        key: 객체 키. 끝이 SES 메시지 ID 입니다.
    """
    # SNS 는 같은 알림을 두 번 보낼 수 있다. Message-ID 는 없는 메일도 있어
    # 지어 넣으므로, 늘 같은 값인 객체 키로 거른다.
    if await store.run(store.processed_keys, [key]):
        print(f"ops 메일 중복 {key}")
        return
    raw = await asyncio.to_thread(get_object, bucket, key)
    # 첨부 디코딩과 HTML 파싱이 무거워 이벤트 루프에서 돌리지 않는다.
    mail = await asyncio.to_thread(mime.parse, raw)

    channel_id = os.environ["OPS_MAIL_SLACK_CHANNEL_ID"]
    client = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
    counterpart = mail.reply_to_address

    thread_ts = await store.run(store.find_thread, channel_id, counterpart)
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

    await store.run(
        store.insert,
        store.OpsMailRow(
            direction="in",
            message_id=mail.message_id,
            ses_message_id=key.rsplit("/", 1)[-1],
            counterpart=counterpart,
            from_address=mail.from_address,
            to_addresses=mail.to_addresses,
            subject=mail.subject,
            body_text=mail.body_text,
            references_ids=mail.references_ids,
            s3_key=key,
            channel_id=channel_id,
            sent_by=None,
        ),
    )
    print(f"ops 메일 수신 {counterpart} {mail.message_id} → {thread_ts}")


@router.post("/ops-mail/sns")
async def handle_sns(request: Request, background: BackgroundTasks) -> dict[str, str]:
    """SNS 가 보낸 요청을 받습니다.

    Args:
        request: SNS 는 Content-Type 을 text/plain 으로 보내서 본문을 직접 읽습니다
        background: 응답을 보낸 뒤 처리를 돌릴 자리

    Returns:
        dict[str, str]: 받았다는 표시
    """
    message = json.loads(await request.body())

    if message.get("TopicArn") != os.environ["OPS_MAIL_SNS_TOPIC_ARN"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="다른 토픽입니다"
        )
    # 인증서를 받아오느라 네트워크를 탄다.
    if not await asyncio.to_thread(sns.is_authentic, message):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="서명이 맞지 않습니다"
        )

    if message["Type"] == "SubscriptionConfirmation":
        response = await asyncio.to_thread(
            requests.get, message["SubscribeURL"], timeout=10
        )
        response.raise_for_status()
        print(f"ops 메일 SNS 구독 확인 {message['TopicArn']}")
        return {"status": "subscribed"}

    if message["Type"] == "Notification":
        notification = json.loads(message["Message"])
        if notification.get("notificationType") == "Received":
            action = notification["receipt"]["action"]
            background.add_task(
                process_object, action["bucketName"], action["objectKey"]
            )

    return {"status": "accepted"}
