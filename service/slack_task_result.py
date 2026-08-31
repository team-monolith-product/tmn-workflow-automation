"""Slack List 작업의 종료 결과 메시지를 한 건으로 유지합니다."""

import asyncio
import uuid
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Literal

from slack_sdk.web.async_client import AsyncWebClient

from service.db import connect, fetch_one


@dataclass(frozen=True)
class SlackTaskResultMessage:
    """한 Slack List 행에 연결된 종료 결과 메시지입니다."""

    list_id: str
    record_id: str
    client_msg_id: str
    channel_id: str
    message_ts: str
    permalink: str | None


RESULT_MESSAGE_COLUMNS = tuple(field.name for field in fields(SlackTaskResultMessage))

SELECT_RESULT_MESSAGE = f"""
SELECT {", ".join(RESULT_MESSAGE_COLUMNS)}
FROM slack_task_result_message
WHERE list_id = %(list_id)s AND record_id = %(record_id)s
"""

UPSERT_RESULT_MESSAGE = f"""
INSERT INTO slack_task_result_message ({", ".join(RESULT_MESSAGE_COLUMNS)})
VALUES ({", ".join(f"%({name})s" for name in RESULT_MESSAGE_COLUMNS)})
ON CONFLICT (list_id, record_id) DO UPDATE SET
    client_msg_id = EXCLUDED.client_msg_id,
    channel_id = EXCLUDED.channel_id,
    message_ts = EXCLUDED.message_ts,
    permalink = EXCLUDED.permalink
"""


def result_client_msg_id(list_id: str, record_id: str) -> str:
    """Slack List 행마다 고정된 종료 결과 메시지 ID를 만듭니다."""
    key = f"https://wfa.codle.io/slack-task-result/{list_id}/{record_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def find_result_message(list_id: str, record_id: str) -> SlackTaskResultMessage | None:
    """저장된 종료 결과 메시지를 조회합니다."""
    with connect(read_only=True) as conn:
        row = fetch_one(
            conn,
            SELECT_RESULT_MESSAGE,
            {"list_id": list_id, "record_id": record_id},
        )
    return SlackTaskResultMessage(**row) if row else None


def save_result_message(message: SlackTaskResultMessage) -> None:
    """게시된 종료 결과 메시지의 Slack 식별자를 저장합니다."""
    with connect() as conn:
        conn.execute(UPSERT_RESULT_MESSAGE, asdict(message))


async def publish_result_message(
    client: AsyncWebClient,
    *,
    list_id: str,
    record_id: str,
    channel_id: str,
    thread_ts: str,
    text: str,
    blocks: list[dict[str, Any]],
) -> tuple[SlackTaskResultMessage, Literal["created", "updated"]]:
    """종료 결과를 처음에는 게시하고 이후에는 같은 메시지를 수정합니다."""
    stored = await asyncio.to_thread(find_result_message, list_id, record_id)
    if stored:
        await client.chat_update(
            channel=stored.channel_id,
            ts=stored.message_ts,
            text=text,
            blocks=blocks,
        )
        message = stored
        action: Literal["created", "updated"] = "updated"
    else:
        client_msg_id = result_client_msg_id(list_id, record_id)
        posted = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            client_msg_id=client_msg_id,
            text=text,
            blocks=blocks,
        )
        message = SlackTaskResultMessage(
            list_id=list_id,
            record_id=record_id,
            client_msg_id=client_msg_id,
            channel_id=str(posted["channel"]),
            message_ts=str(posted["ts"]),
            permalink=None,
        )
        await asyncio.to_thread(save_result_message, message)
        action = "created"

    if message.permalink is None:
        response = await client.chat_getPermalink(
            channel=message.channel_id,
            message_ts=message.message_ts,
        )
        message = replace(message, permalink=str(response["permalink"]))
        await asyncio.to_thread(save_result_message, message)

    return message, action
