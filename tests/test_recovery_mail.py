import base64
from email.message import EmailMessage

import pytest

from api.gmail import (
    GmailHistory,
    GmailHistoryExpired,
    GmailMessageNotFound,
    GmailWatch,
)
from service.recovery_mail import (
    CURSOR_KEY,
    RecoveryMailConfig,
    RecoveryMailConfigurationError,
    RecoveryMailService,
    RecoveryMailSyncBusy,
    parse_raw_message,
)


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = False

    async def acquire(self, blocking=False):
        return self.acquired

    async def release(self):
        self.released = True


class FakeRedis:
    def __init__(self, cursor="10", lock_acquired=True):
        self.values = {}
        if cursor is not None:
            self.values[CURSOR_KEY] = cursor
        self.lock_acquired = lock_acquired
        self.last_lock = None

    def lock(self, *args, **kwargs):
        self.last_lock = FakeLock(self.lock_acquired)
        return self.last_lock

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


class FakeGmail:
    def __init__(self, raw_messages):
        self.raw_messages = raw_messages
        self.history = GmailHistory(message_ids=list(raw_messages), history_id="20")
        self.watch_result = GmailWatch(history_id="30", expiration="999")
        self.recent_message_ids = list(raw_messages)
        self.history_error = None

    def watch(self, topic):
        return self.watch_result

    def list_history(self, cursor):
        if self.history_error:
            raise self.history_error
        return self.history

    def list_recent_messages(self, after_epoch):
        return self.recent_message_ids

    def get_raw_message(self, message_id):
        if message_id == "deleted":
            raise GmailMessageNotFound("get_message", 404)
        return self.raw_messages[message_id]


class FakeSlack:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def chat_postMessage(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)
        return {"ok": True}


def config(**overrides):
    values = {
        "enabled": True,
        "mailbox_email": "funzero@team-mono.com",
        "allowed_senders": frozenset({"allowed@example.com"}),
        "slack_channel_id": "C123",
        "pubsub_topic": "projects/test/topics/recovery-mail",
        "pubsub_push_service_account": "push@test.iam.gserviceaccount.com",
        "pubsub_oidc_audience": "https://wfa.example.com/webhooks/recovery-mail",
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
        "oauth_refresh_token": "refresh-token",
        "slack_bot_token": "slack-token",
        "redis_url": "redis://localhost:6379",
        "redis_password": "redis-password",
    }
    values.update(overrides)
    return RecoveryMailConfig(**values)


def raw_message(
    sender, subject="인증 코드", plain="인증 코드는 123456입니다.", html=None
):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "funzero@team-mono.com"
    message["Subject"] = subject
    if plain is not None:
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype="html")
    else:
        message.set_content(html or "", subtype="html")
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def test_enabled_config_requires_runtime_values():
    with pytest.raises(RecoveryMailConfigurationError):
        RecoveryMailConfig.from_env({"RECOVERY_MAIL_ENABLED": "true"})


def test_disabled_config_allows_empty_runtime_values():
    loaded = RecoveryMailConfig.from_env({})

    assert loaded.enabled is False
    assert loaded.allowed_senders == frozenset()


def test_config_rejects_invalid_code_pattern():
    with pytest.raises(RecoveryMailConfigurationError):
        RecoveryMailConfig.from_env({"RECOVERY_MAIL_CODE_PATTERN": "["})


def test_parse_raw_message_prefers_plain_text():
    parsed = parse_raw_message(
        "m1",
        raw_message(
            "Example <allowed@example.com>",
            plain="  인증 코드는   123456 입니다.  ",
            html="<p>다른 코드 999999</p>",
        ),
    )

    assert parsed.sender == "allowed@example.com"
    assert parsed.subject == "인증 코드"
    assert parsed.body == "인증 코드는 123456 입니다."


def test_parse_raw_message_falls_back_to_html():
    parsed = parse_raw_message(
        "m1",
        raw_message(
            "allowed@example.com",
            plain=None,
            html="<p>인증 코드는 <strong>123456</strong>입니다.</p>",
        ),
    )

    assert "123456" in parsed.body


@pytest.mark.asyncio
async def test_sync_forwards_only_allowed_senders_and_advances_cursor():
    gmail = FakeGmail(
        {
            "allowed": raw_message("Allowed <allowed@example.com>"),
            "ignored": raw_message("ignored@example.com"),
        }
    )
    redis_client = FakeRedis()
    slack = FakeSlack()
    service = RecoveryMailService(
        config=config(),
        redis_client=redis_client,
        gmail_client=gmail,
        slack_client=slack,
    )

    result = await service.sync()

    assert result.inspected == 2
    assert result.forwarded == 1
    assert redis_client.values[CURSOR_KEY] == "20"
    assert len(slack.calls) == 1
    assert slack.calls[0]["channel"] == "C123"
    assert "123456" in slack.calls[0]["text"]
    assert "인증 코드는" not in slack.calls[0]["text"]
    assert redis_client.last_lock.released is True


@pytest.mark.asyncio
async def test_sync_skips_allowed_mail_without_matching_code():
    gmail = FakeGmail(
        {"allowed": raw_message("allowed@example.com", plain="인증 링크를 확인하세요.")}
    )
    redis_client = FakeRedis()
    slack = FakeSlack()
    service = RecoveryMailService(
        config=config(),
        redis_client=redis_client,
        gmail_client=gmail,
        slack_client=slack,
    )

    result = await service.sync()

    assert result.forwarded == 0
    assert slack.calls == []
    assert redis_client.values[CURSOR_KEY] == "20"


@pytest.mark.asyncio
async def test_sync_skips_message_deleted_after_history_read():
    gmail = FakeGmail({"allowed": raw_message("allowed@example.com")})
    gmail.history = GmailHistory(message_ids=["deleted", "allowed"], history_id="20")
    redis_client = FakeRedis()
    slack = FakeSlack()
    service = RecoveryMailService(
        config=config(),
        redis_client=redis_client,
        gmail_client=gmail,
        slack_client=slack,
    )

    result = await service.sync()

    assert result.inspected == 2
    assert result.forwarded == 1
    assert redis_client.values[CURSOR_KEY] == "20"


@pytest.mark.asyncio
async def test_sync_does_not_advance_cursor_when_slack_fails():
    gmail = FakeGmail({"allowed": raw_message("allowed@example.com")})
    redis_client = FakeRedis()
    service = RecoveryMailService(
        config=config(),
        redis_client=redis_client,
        gmail_client=gmail,
        slack_client=FakeSlack(error=RuntimeError("slack failed")),
    )

    with pytest.raises(RuntimeError, match="slack failed"):
        await service.sync()

    assert redis_client.values[CURSOR_KEY] == "10"
    assert redis_client.last_lock.released is True


@pytest.mark.asyncio
async def test_expired_cursor_recovers_recent_messages():
    gmail = FakeGmail({"allowed": raw_message("allowed@example.com")})
    gmail.history_error = GmailHistoryExpired("list_history", 404)
    redis_client = FakeRedis()
    slack = FakeSlack()
    service = RecoveryMailService(
        config=config(),
        redis_client=redis_client,
        gmail_client=gmail,
        slack_client=slack,
    )

    result = await service.sync()

    assert result.recovered is True
    assert result.forwarded == 1
    assert redis_client.values[CURSOR_KEY] == "30"


@pytest.mark.asyncio
async def test_sync_rejects_concurrent_run():
    service = RecoveryMailService(
        config=config(),
        redis_client=FakeRedis(lock_acquired=False),
        gmail_client=FakeGmail({}),
        slack_client=FakeSlack(),
    )

    with pytest.raises(RecoveryMailSyncBusy):
        await service.sync()
