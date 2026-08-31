"""Slack 작업 종료 결과 메시지의 중복 방지 테스트."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from service.slack_task_result import (
    RESULT_MESSAGE_COLUMNS,
    SlackTaskResultMessage,
    publish_result_message,
    result_client_msg_id,
)

LIST_ID = "F0BTVRW2AAU"
RECORD_ID = "Rec0BSXEKFH1T"
CHANNEL = "C0ABCDE1234"
ROOT_TS = "1700000000.000100"
MESSAGE_TS = "1700000004.000200"
PERMALINK = "https://example.slack.com/archives/C0ABCDE1234/p1700000004000200"
MIGRATION = "migrations/knowledge/006_slack_task_result.sql"


def stored_message(permalink: str | None = PERMALINK) -> SlackTaskResultMessage:
    return SlackTaskResultMessage(
        list_id=LIST_ID,
        record_id=RECORD_ID,
        client_msg_id=result_client_msg_id(LIST_ID, RECORD_ID),
        channel_id=CHANNEL,
        message_ts=MESSAGE_TS,
        permalink=permalink,
    )


def test_result_message_columns_exist_in_migration():
    sql = (Path(__file__).parent.parent / MIGRATION).read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s{4}(\w+)\s", sql, re.M))

    assert set(RESULT_MESSAGE_COLUMNS) <= declared


def test_client_msg_id_is_stable_per_list_row():
    first = result_client_msg_id(LIST_ID, RECORD_ID)

    assert first == result_client_msg_id(LIST_ID, RECORD_ID)
    assert first != result_client_msg_id(LIST_ID, "RecOTHER")
    assert len(first) == 36


async def test_first_publish_saves_ts_then_permalink():
    client = AsyncMock()
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": MESSAGE_TS}
    client.chat_getPermalink.return_value = {"permalink": PERMALINK}
    saved: list[SlackTaskResultMessage] = []

    with patch(
        "service.slack_task_result.find_result_message", return_value=None
    ), patch(
        "service.slack_task_result.save_result_message",
        side_effect=saved.append,
    ):
        message, action = await publish_result_message(
            client,
            list_id=LIST_ID,
            record_id=RECORD_ID,
            channel_id=CHANNEL,
            thread_ts=ROOT_TS,
            text="완료",
            blocks=[],
        )

    assert action == "created"
    assert client.chat_postMessage.await_args.kwargs["client_msg_id"] == (
        result_client_msg_id(LIST_ID, RECORD_ID)
    )
    assert saved[0].message_ts == MESSAGE_TS
    assert saved[0].permalink is None
    assert saved[1].permalink == PERMALINK
    assert message == saved[1]


async def test_retry_updates_saved_message_instead_of_posting():
    client = AsyncMock()
    stored = stored_message()

    with patch(
        "service.slack_task_result.find_result_message", return_value=stored
    ), patch("service.slack_task_result.save_result_message", Mock()) as save:
        message, action = await publish_result_message(
            client,
            list_id=LIST_ID,
            record_id=RECORD_ID,
            channel_id=CHANNEL,
            thread_ts=ROOT_TS,
            text="수정된 완료 결과",
            blocks=[{"type": "section"}],
        )

    client.chat_postMessage.assert_not_awaited()
    client.chat_update.assert_awaited_once_with(
        channel=CHANNEL,
        ts=MESSAGE_TS,
        text="수정된 완료 결과",
        blocks=[{"type": "section"}],
    )
    client.chat_getPermalink.assert_not_awaited()
    save.assert_not_called()
    assert action == "updated"
    assert message == stored


async def test_ts_is_saved_before_permalink_lookup_failure():
    client = AsyncMock()
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": MESSAGE_TS}
    client.chat_getPermalink.side_effect = RuntimeError("permalink unavailable")
    saved: list[SlackTaskResultMessage] = []

    with patch(
        "service.slack_task_result.find_result_message", return_value=None
    ), patch("service.slack_task_result.save_result_message", side_effect=saved.append):
        with pytest.raises(RuntimeError, match="permalink unavailable"):
            await publish_result_message(
                client,
                list_id=LIST_ID,
                record_id=RECORD_ID,
                channel_id=CHANNEL,
                thread_ts=ROOT_TS,
                text="완료",
                blocks=[],
            )

    assert len(saved) == 1
    assert saved[0].message_ts == MESSAGE_TS
    assert saved[0].permalink is None
