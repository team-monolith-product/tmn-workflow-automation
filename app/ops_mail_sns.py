"""
ops@team-mono.com 으로 온 메일을 받는 SNS 웹훅입니다.

    외부 → ops@team-mono.com → (구글 라우팅) → ops@in.team-mono.com
    → SES 수신 규칙 → S3 원본 저장 + SNS 알림 → 여기

SES 는 임의 URL 로 요청을 보내지 못해서 SNS 의 HTTPS 구독으로 받습니다.
구독을 만들면 SubscriptionConfirmation 이 한 번 오고, SubscribeURL 을 열어야
구독이 살아납니다.

여기서는 요청이 진짜인지만 확인하고, 메일을 슬랙에 올리는 것은
service/ops_mail/inbound.py 가 합니다. 처리 중 실패한 메일은
scripts/reconcile_ops_mail.py 가 S3 를 훑어 다시 처리합니다.

응답을 먼저 돌려주고 처리는 그 뒤에 합니다. 응답이 늦으면 SNS 가 실패로 보고
같은 알림을 다시 보냅니다.
"""

import asyncio
import json
import os

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from service.ops_mail import sns
from service.ops_mail.inbound import process_object

router = APIRouter()


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
