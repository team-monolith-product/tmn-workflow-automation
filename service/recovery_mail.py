"""복구 계정의 Gmail 변경분을 Slack으로 전달합니다."""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import Message
from email.utils import parseaddr
from typing import Literal, Mapping

import redis.asyncio as redis
from bs4 import BeautifulSoup
from redis.asyncio.lock import Lock
from redis.exceptions import LockError
from slack_sdk.web.async_client import AsyncWebClient

from api.gmail import (
    GmailClient,
    GmailHistoryExpired,
    GmailMessageNotFound,
    GmailWatch,
)

CURSOR_KEY = "workflow_automation/recovery_mail/history_id"
SYNC_LOCK_KEY = "workflow_automation/recovery_mail/sync_lock"


class RecoveryMailConfigurationError(RuntimeError):
    """활성화된 기능에 필요한 환경 변수가 없습니다."""


class RecoveryMailSyncBusy(RuntimeError):
    """다른 프로세스가 Gmail 변경분을 처리하고 있습니다."""


@dataclass(frozen=True)
class RecoveryMailConfig:
    enabled: bool
    mailbox_email: str
    allowed_senders: frozenset[str]
    slack_channel_id: str
    pubsub_topic: str
    pubsub_push_service_account: str
    pubsub_oidc_audience: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_refresh_token: str
    slack_bot_token: str
    redis_url: str
    redis_password: str
    recovery_lookback_seconds: int = 3600
    code_pattern: str = r"(?<!\d)\d{4,8}(?!\d)"
    sync_lock_seconds: int = 120

    @classmethod
    def from_env(
        cls, environment: Mapping[str, str] | None = None
    ) -> "RecoveryMailConfig":
        env = os.environ if environment is None else environment
        enabled = env.get("RECOVERY_MAIL_ENABLED", "false").lower() == "true"

        values = {
            "mailbox_email": env.get("RECOVERY_MAILBOX_EMAIL", "").strip(),
            "slack_channel_id": env.get("RECOVERY_MAIL_SLACK_CHANNEL_ID", "").strip(),
            "pubsub_topic": env.get("GMAIL_PUBSUB_TOPIC", "").strip(),
            "pubsub_push_service_account": env.get(
                "GMAIL_PUBSUB_PUSH_SERVICE_ACCOUNT", ""
            ).strip(),
            "pubsub_oidc_audience": env.get("GMAIL_PUBSUB_OIDC_AUDIENCE", "").strip(),
            "oauth_client_id": env.get("GMAIL_OAUTH_CLIENT_ID", "").strip(),
            "oauth_client_secret": env.get("GMAIL_OAUTH_CLIENT_SECRET", "").strip(),
            "oauth_refresh_token": env.get("GMAIL_OAUTH_REFRESH_TOKEN", "").strip(),
            "slack_bot_token": env.get("SLACK_BOT_TOKEN", "").strip(),
            "redis_url": env.get("REDIS_URL", "").strip(),
        }
        allowed_senders = frozenset(
            sender.strip().lower()
            for sender in env.get("RECOVERY_MAIL_ALLOWED_SENDERS", "").split(",")
            if sender.strip()
        )
        code_pattern = env.get("RECOVERY_MAIL_CODE_PATTERN", r"(?<!\d)\d{4,8}(?!\d)")
        try:
            re.compile(code_pattern)
        except re.error as exc:
            raise RecoveryMailConfigurationError(
                "RECOVERY_MAIL_CODE_PATTERN 값이 올바른 정규식이 아닙니다."
            ) from exc

        if enabled:
            missing = [name for name, value in values.items() if not value]
            if not allowed_senders:
                missing.append("allowed_senders")
            if missing:
                raise RecoveryMailConfigurationError(
                    f"복구 메일 환경 변수가 부족합니다: {', '.join(sorted(missing))}"
                )

        return cls(
            enabled=enabled,
            allowed_senders=allowed_senders,
            redis_password=env.get("REDIS_PASSWORD", ""),
            recovery_lookback_seconds=_positive_int(
                env, "RECOVERY_MAIL_LOOKBACK_SECONDS", 3600
            ),
            code_pattern=code_pattern,
            sync_lock_seconds=_positive_int(
                env, "RECOVERY_MAIL_SYNC_LOCK_SECONDS", 120
            ),
            **values,
        )


@dataclass(frozen=True)
class ParsedRecoveryMail:
    message_id: str
    sender: str
    subject: str
    body: str


@dataclass(frozen=True)
class RecoveryMailSyncResult:
    inspected: int
    forwarded: int
    history_id: str
    recovered: bool = False


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw_value = environment.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RecoveryMailConfigurationError(f"{name} 값은 정수여야 합니다.") from exc
    if value <= 0:
        raise RecoveryMailConfigurationError(f"{name} 값은 0보다 커야 합니다.")
    return value


def parse_raw_message(message_id: str, encoded_raw: str) -> ParsedRecoveryMail:
    """Gmail raw 응답에서 Slack에 전달할 텍스트를 꺼냅니다."""
    padding = "=" * (-len(encoded_raw) % 4)
    raw_message = base64.urlsafe_b64decode(encoded_raw + padding)
    message = message_from_bytes(raw_message, policy=policy.default)
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    subject = str(message.get("Subject", "")).strip() or "제목 없음"
    body = _message_body(message)
    return ParsedRecoveryMail(
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
    )


def _message_body(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            continue
        if not isinstance(content, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(content)
        else:
            html_parts.append(content)

    body = "\n".join(plain_parts)
    if not body.strip() and html_parts:
        body = BeautifulSoup("\n".join(html_parts), "lxml").get_text("\n")

    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


class RecoveryMailService:
    """Gmail cursor를 기준으로 새 메일을 직렬 처리합니다."""

    def __init__(
        self,
        *,
        config: RecoveryMailConfig,
        redis_client: redis.Redis,
        gmail_client: GmailClient,
        slack_client: AsyncWebClient,
    ) -> None:
        self.config = config
        self.redis = redis_client
        self.gmail = gmail_client
        self.slack = slack_client

    async def renew_watch(self) -> GmailWatch:
        lock = await self._acquire_lock()
        try:
            watch = await asyncio.to_thread(self.gmail.watch, self.config.pubsub_topic)
            cursor = await self.redis.get(CURSOR_KEY)
            if cursor is None:
                await self._recover_recent(watch.history_id)
            return watch
        finally:
            await self._release_lock(lock)

    async def sync(self) -> RecoveryMailSyncResult:
        lock = await self._acquire_lock()
        try:
            cursor = await self.redis.get(CURSOR_KEY)
            if cursor is None:
                watch = await asyncio.to_thread(
                    self.gmail.watch, self.config.pubsub_topic
                )
                return await self._recover_recent(watch.history_id)

            try:
                history = await asyncio.to_thread(self.gmail.list_history, str(cursor))
            except GmailHistoryExpired:
                watch = await asyncio.to_thread(
                    self.gmail.watch, self.config.pubsub_topic
                )
                return await self._recover_recent(watch.history_id)

            forwarded = await self._forward_messages(history.message_ids)
            await self.redis.set(CURSOR_KEY, history.history_id)
            return RecoveryMailSyncResult(
                inspected=len(history.message_ids),
                forwarded=forwarded,
                history_id=history.history_id,
            )
        finally:
            await self._release_lock(lock)

    async def _recover_recent(self, current_history_id: str) -> RecoveryMailSyncResult:
        after_epoch = int(datetime.now(timezone.utc).timestamp()) - (
            self.config.recovery_lookback_seconds
        )
        message_ids = await asyncio.to_thread(
            self.gmail.list_recent_messages, after_epoch
        )
        forwarded = await self._forward_messages(list(reversed(message_ids)))
        await self.redis.set(CURSOR_KEY, current_history_id)
        return RecoveryMailSyncResult(
            inspected=len(message_ids),
            forwarded=forwarded,
            history_id=current_history_id,
            recovered=True,
        )

    async def _forward_messages(self, message_ids: list[str]) -> int:
        forwarded = 0
        code_pattern = re.compile(self.config.code_pattern)
        for message_id in message_ids:
            try:
                encoded_raw = await asyncio.to_thread(
                    self.gmail.get_raw_message, message_id
                )
            except GmailMessageNotFound:
                continue
            mail = parse_raw_message(message_id, encoded_raw)
            if mail.sender not in self.config.allowed_senders:
                continue

            codes = _extract_codes(code_pattern, mail.body)
            if not codes:
                continue
            text = (
                "복구 계정 인증 메일\n"
                f"보낸 사람: {mail.sender}\n"
                f"제목: {mail.subject}\n"
                f"인증번호: {', '.join(codes)}"
            ).strip()
            await self.slack.chat_postMessage(
                channel=self.config.slack_channel_id,
                text=text,
                mrkdwn=False,
                unfurl_links=False,
                unfurl_media=False,
            )
            forwarded += 1
        return forwarded

    async def _acquire_lock(self) -> Lock:
        lock = self.redis.lock(
            SYNC_LOCK_KEY,
            timeout=self.config.sync_lock_seconds,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            raise RecoveryMailSyncBusy("복구 메일 동기화가 이미 실행 중입니다.")
        return lock

    @staticmethod
    async def _release_lock(lock: Lock) -> None:
        try:
            await lock.release()
        except LockError:
            pass


def _extract_codes(pattern: re.Pattern[str], body: str) -> list[str]:
    codes: list[str] = []
    for match in pattern.finditer(body):
        if "code" in pattern.groupindex:
            value = match.group("code")
        elif pattern.groups == 1:
            value = match.group(1)
        else:
            value = match.group(0)
        if value and value not in codes:
            codes.append(value)
    return codes


def recovery_mail_enabled() -> bool:
    return os.environ.get("RECOVERY_MAIL_ENABLED", "false").lower() == "true"


async def renew_recovery_mail_watch() -> GmailWatch | None:
    config = RecoveryMailConfig.from_env()
    if not config.enabled:
        return None
    return await _run_with_clients(config, "renew")


async def sync_recovery_mail() -> RecoveryMailSyncResult | None:
    config = RecoveryMailConfig.from_env()
    if not config.enabled:
        return None
    return await _run_with_clients(config, "sync")


async def _run_with_clients(
    config: RecoveryMailConfig, operation: Literal["renew", "sync"]
) -> GmailWatch | RecoveryMailSyncResult:
    redis_client = redis.Redis.from_url(
        config.redis_url,
        password=config.redis_password,
        decode_responses=True,
    )
    gmail_client = GmailClient(
        mailbox_email=config.mailbox_email,
        client_id=config.oauth_client_id,
        client_secret=config.oauth_client_secret,
        refresh_token=config.oauth_refresh_token,
    )
    slack_client = AsyncWebClient(token=config.slack_bot_token)
    service = RecoveryMailService(
        config=config,
        redis_client=redis_client,
        gmail_client=gmail_client,
        slack_client=slack_client,
    )
    try:
        if operation == "renew":
            return await service.renew_watch()
        return await service.sync()
    finally:
        await redis_client.aclose()
        await asyncio.to_thread(gmail_client.close)
