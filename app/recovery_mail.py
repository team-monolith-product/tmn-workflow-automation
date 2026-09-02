"""Gmail Pub/Sub push webhook을 제공합니다."""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Response, status
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from service.recovery_mail import (
    RecoveryMailConfig,
    RecoveryMailSyncBusy,
    sync_recovery_mail,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhooks/recovery-mail", status_code=status.HTTP_204_NO_CONTENT)
async def receive_recovery_mail_notification(
    payload: dict,
    authorization: str | None = Header(default=None),
) -> Response:
    """인증된 Pub/Sub 알림을 받아 Gmail 변경분을 처리합니다."""
    config = RecoveryMailConfig.from_env()
    if not config.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    token = _bearer_token(authorization)
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token,
            GoogleRequest(),
            config.pubsub_oidc_audience,
        )
    except (GoogleAuthError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc
    if (
        claims.get("email") != config.pubsub_push_service_account
        or claims.get("email_verified") is not True
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    notification = _decode_notification(payload)
    if notification.get("emailAddress", "").lower() != config.mailbox_email.lower():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        await sync_recovery_mail()
    except RecoveryMailSyncBusy as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    except Exception as exc:
        logger.error("복구 메일 동기화 실패: %s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return token


def _decode_notification(payload: dict) -> dict:
    encoded_data = payload.get("message", {}).get("data")
    if not isinstance(encoded_data, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    try:
        padding = "=" * (-len(encoded_data) % 4)
        decoded = base64.urlsafe_b64decode(encoded_data + padding)
        notification = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
    if not isinstance(notification, dict) or not notification.get("historyId"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return notification
