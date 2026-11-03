"""
ops@team-mono.com 으로 온 메일을 받아 슬랙에 올리는 엔드포인트입니다.

    외부 → ops@team-mono.com → (구글 라우팅) → ops@in.team-mono.com
    → SES 수신 규칙 → S3 원본 저장 + SNS 알림 → 여기

SES 는 임의 URL 로 요청을 보내지 못해서 SNS 의 HTTPS 구독으로 받습니다.
구독을 만들면 SubscriptionConfirmation 이 한 번 오고, SubscribeURL 을 열어야
구독이 살아납니다.

슬랙 스레드는 상대방 주소 하나에 하나입니다. 처음 오가는 상대면 채널에
상대 주소로 스레드를 열고, 메일은 늘 그 스레드의 댓글로 답니다. 우리가 새 메일로
먼저 보낸 상대라면 그 메일들을 스레드 첫 메시지에 보여주고 스레드에 묶습니다.

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
from app.ops_mail import escape, run_store
from service.ops_mail import mime, sns, store

router = APIRouter()

# 슬랙 메시지 한 개에 싣는 본문 길이. 넘으면 자르고 원본 위치를 남긴다.
MAX_BODY_CHARS = 3500


def format_inbound(mail: mime.ParsedMail, receipt: dict[str, Any], s3_uri: str) -> str:
    """받은 메일을 슬랙 메시지로 만듭니다.

    Args:
        mail: 읽은 메일
        receipt: SES 알림의 receipt (스팸·바이러스 판정)
        s3_uri: 원본 위치

    Returns:
        str: 슬랙 메시지 텍스트
    """
    warnings = []
    if receipt["spamVerdict"]["status"] == "FAIL":
        warnings.append("⚠️ 스팸 의심")
    if receipt["virusVerdict"]["status"] == "FAIL":
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
        f" ({mail['created_at']:%m/%d %H:%M}, <@{mail['sent_by']}> 님)"
        for mail in sent
    ]
    return "\n".join(lines)


async def mail_thread(client: AsyncWebClient, channel: str, counterpart: str) -> str:
    """상대방 스레드를 찾고, 처음이면 엽니다.

    Args:
        client: 슬랙 클라이언트
        channel: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        str: 스레드 ts
    """
    thread_ts = await asyncio.to_thread(
        run_store, store.find_thread, channel, counterpart
    )
    if thread_ts:
        return thread_ts
    sent = await asyncio.to_thread(run_store, store.unthreaded, channel, counterpart)
    response = await client.chat_postMessage(
        channel=channel, text=format_thread_root(counterpart, sent)
    )
    await asyncio.to_thread(
        run_store, store.attach_thread, channel, counterpart, response["ts"]
    )
    return response["ts"]


async def process_notification(notification: dict[str, Any]) -> None:
    """SES 수신 알림 하나를 처리합니다. S3 와 DB 는 동기라 스레드로 넘깁니다.

    Args:
        notification: SNS Message 안의 SES 알림
    """
    action = notification["receipt"]["action"]
    s3_uri = f"s3://{action['bucketName']}/{action['objectKey']}"
    raw = await asyncio.to_thread(get_object, action["bucketName"], action["objectKey"])
    mail = mime.parse(raw)

    if await asyncio.to_thread(run_store, store.exists, mail.message_id):
        print(f"ops 메일 중복 알림 {mail.message_id}")
        return

    channel_id = os.environ["OPS_MAIL_SLACK_CHANNEL_ID"]
    client = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
    counterpart = mail.reply_to_address
    thread_ts = await mail_thread(client, channel_id, counterpart)
    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=format_inbound(mail, notification["receipt"], s3_uri),
    )
    if mail.attachments and notification["receipt"]["virusVerdict"]["status"] != "FAIL":
        await client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            file_uploads=[
                {"file": a.data, "filename": a.filename, "title": a.filename}
                for a in mail.attachments
            ],
        )

    await asyncio.to_thread(
        run_store,
        store.insert,
        store.OpsMailRow(
            direction="in",
            message_id=mail.message_id,
            ses_message_id=notification["mail"]["messageId"],
            counterpart=counterpart,
            from_address=mail.from_address,
            to_addresses=mail.to_addresses,
            subject=mail.subject,
            body_text=mail.body_text,
            references_ids=mail.references_ids,
            s3_key=action["objectKey"],
            channel_id=channel_id,
            thread_ts=thread_ts,
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
            background.add_task(process_notification, notification)

    return {"status": "accepted"}
