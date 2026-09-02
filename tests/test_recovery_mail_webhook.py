import base64
import json
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from starlette.testclient import TestClient

from app import recovery_mail as recovery_mail_app
from service.recovery_mail import RecoveryMailConfig, RecoveryMailSyncBusy


def config():
    return RecoveryMailConfig(
        enabled=True,
        mailbox_email="funzero@team-mono.com",
        allowed_senders=frozenset({"allowed@example.com"}),
        slack_channel_id="C123",
        pubsub_topic="projects/test/topics/recovery-mail",
        pubsub_push_service_account="push@test.iam.gserviceaccount.com",
        pubsub_oidc_audience="https://wfa.example.com/webhooks/recovery-mail",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_refresh_token="refresh-token",
        slack_bot_token="slack-token",
        redis_url="redis://localhost:6379",
        redis_password="redis-password",
    )


def payload(email="funzero@team-mono.com", history_id="123"):
    data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "pubsub-message"}}


def client():
    app = FastAPI()
    app.include_router(recovery_mail_app.router)
    return TestClient(app)


def test_webhook_validates_push_and_starts_sync():
    sync = AsyncMock()
    with (
        patch.object(
            recovery_mail_app.RecoveryMailConfig, "from_env", return_value=config()
        ),
        patch.object(
            recovery_mail_app.id_token,
            "verify_oauth2_token",
            return_value={
                "email": "push@test.iam.gserviceaccount.com",
                "email_verified": True,
            },
        ),
        patch.object(recovery_mail_app, "sync_recovery_mail", sync),
        client() as test_client,
    ):
        response = test_client.post(
            "/webhooks/recovery-mail",
            json=payload(),
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 204
    sync.assert_awaited_once()


def test_webhook_rejects_other_mailbox():
    sync = AsyncMock()
    with (
        patch.object(
            recovery_mail_app.RecoveryMailConfig, "from_env", return_value=config()
        ),
        patch.object(
            recovery_mail_app.id_token,
            "verify_oauth2_token",
            return_value={
                "email": "push@test.iam.gserviceaccount.com",
                "email_verified": True,
            },
        ),
        patch.object(recovery_mail_app, "sync_recovery_mail", sync),
        client() as test_client,
    ):
        response = test_client.post(
            "/webhooks/recovery-mail",
            json=payload(email="other@example.com"),
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 400
    sync.assert_not_awaited()


def test_webhook_rejects_invalid_oidc_token():
    sync = AsyncMock()
    with (
        patch.object(
            recovery_mail_app.RecoveryMailConfig, "from_env", return_value=config()
        ),
        patch.object(
            recovery_mail_app.id_token,
            "verify_oauth2_token",
            side_effect=ValueError("invalid token"),
        ),
        patch.object(recovery_mail_app, "sync_recovery_mail", sync),
        client() as test_client,
    ):
        response = test_client.post(
            "/webhooks/recovery-mail",
            json=payload(),
            headers={"Authorization": "Bearer invalid-token"},
        )

    assert response.status_code == 401
    sync.assert_not_awaited()


def test_webhook_returns_retryable_error_when_sync_is_busy():
    with (
        patch.object(
            recovery_mail_app.RecoveryMailConfig, "from_env", return_value=config()
        ),
        patch.object(
            recovery_mail_app.id_token,
            "verify_oauth2_token",
            return_value={
                "email": "push@test.iam.gserviceaccount.com",
                "email_verified": True,
            },
        ),
        patch.object(
            recovery_mail_app,
            "sync_recovery_mail",
            AsyncMock(side_effect=RecoveryMailSyncBusy()),
        ),
        client() as test_client,
    ):
        response = test_client.post(
            "/webhooks/recovery-mail",
            json=payload(),
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 503
