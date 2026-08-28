"""Slack List 작업 시작과 종료 요약 테스트."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from service.slack_task_thread import (
    find_work_thread_column_id,
    message_location,
    parse_slack_list_task_url,
    publish_task_result,
    start_task_from_slack_list,
    task_list_schema,
)

LIST_ID = "F0BTVRW2AAU"
RECORD_ID = "Rec0BSXEKFH1T"
CHANNEL = "C0ABCDE1234"
LIST_URL = (
    "https://monolith-keb2010.slack.com/lists/T02F55Y6M4M/"
    f"{LIST_ID}?record_id={RECORD_ID}"
)
ROOT_TS = "1700000000.000100"
ROOT_URL = "https://example.slack.com/archives/C0ABCDE1234/p1700000000000100"
SOURCE_TS = "1699999999.000000"
SOURCE_URL = "https://example.slack.com/archives/C0ABCDE1234/p1699999999000000"
SOURCE_REF = {"value": SOURCE_URL, "channel_id": CHANNEL, "ts": SOURCE_TS}

SCHEMA = [
    {
        "id": "ColTitle",
        "key": "name",
        "name": "작업",
        "type": "text",
        "is_primary_column": True,
    },
    {
        "id": "ColSource",
        "key": "slack_thread",
        "name": "요청 맥락",
        "type": "message",
    },
    {
        "id": "ColWork",
        "key": "work_thread",
        "name": "작업 기록",
        "type": "message",
    },
    {"id": "ColDone", "key": "todo_completed", "type": "todo_completed"},
    {"id": "ColOwner", "key": "todo_assignee", "type": "todo_assignee"},
    {"id": "ColDue", "key": "todo_due_date", "type": "todo_due_date"},
]


def record(work=None, source=None, completed=False) -> dict:
    fields = [
        {"column_id": "ColTitle", "text": "교육생 계정 일괄 생성"},
        {"column_id": "ColOwner", "user": ["U01OWNER"]},
        {"column_id": "ColDue", "date": ["2026-09-02"]},
        {"column_id": "ColDone", "checkbox": [completed]},
    ]
    if source is not None:
        fields.append({"column_id": "ColSource", "message": source})
    if work is not None:
        fields.append({"column_id": "ColWork", "message": work})
    return {"id": RECORD_ID, "fields": fields}


def info(item: dict, schema: list[dict] | None = None) -> dict:
    return {
        "list": {"list_metadata": {"schema": schema or SCHEMA}},
        "record": item,
    }


def test_list_url_parser_uses_list_and_record_ids():
    parsed = parse_slack_list_task_url(LIST_URL)

    assert parsed.list_id == LIST_ID
    assert parsed.record_id == RECORD_ID


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/lists/T1/F012?record_id=Rec01",
        "https://example.slack.com/lists/T1/F012",
        "https://example.slack.com/archives/C01/p1700000000000000",
    ],
)
def test_list_url_parser_rejects_non_record_links(value):
    with pytest.raises(ValueError):
        parse_slack_list_task_url(value)


def test_work_column_can_be_discovered_by_visible_name():
    manual = [
        {
            "id": "ColManual",
            "key": "random_key",
            "name": "작업 기록",
            "type": "message",
        }
    ]

    assert find_work_thread_column_id(manual) == "ColManual"


def test_duplicate_work_columns_are_rejected():
    duplicate = SCHEMA + [
        {
            "id": "ColOther",
            "key": "random_key",
            "name": "작업 기록",
            "type": "message",
        }
    ]

    with pytest.raises(ValueError, match="여러 개"):
        find_work_thread_column_id(duplicate)


def test_schema_reads_message_objects_arrays_and_legacy_urls():
    schema = task_list_schema(SCHEMA)
    item = record(
        source=[
            ROOT_URL,
            {"value": ROOT_URL},
        ],
        work={"channel_id": CHANNEL, "ts": ROOT_TS},
    )

    assert schema.source_thread_references_of(item) == [
        {"value": ROOT_URL},
        {"value": ROOT_URL},
    ]
    assert schema.work_thread_references_of(item) == [
        {"channel_id": CHANNEL, "ts": ROOT_TS}
    ]


def test_message_permalink_uses_thread_root():
    location = message_location(
        {
            "value": (
                "https://example.slack.com/archives/C0ABCDE1234/"
                "p1700000004000200?thread_ts=1700000000.000100&cid=C0ABCDE1234"
            )
        }
    )

    assert location.channel_id == CHANNEL
    assert location.ts == "1700000004.000200"
    assert location.root_ts == ROOT_TS


def test_message_permalink_accepts_private_channel_id():
    location = message_location(
        {
            "value": (
                "https://example.slack.com/archives/G0ABCDE1234/" "p1700000004000200"
            )
        }
    )

    assert location.channel_id == "G0ABCDE1234"
    assert location.ts == "1700000004.000200"
    assert location.root_ts == "1700000004.000200"


async def test_start_creates_one_root_and_stores_its_permalink():
    client = AsyncMock()
    client.slackLists_items_info.side_effect = [
        info(record(source=SOURCE_REF)),
        info(record(source=SOURCE_REF)),
    ]
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": ROOT_TS}
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}
    client.conversations_replies.return_value = {
        "messages": [{"bot_id": "B01BOT", "ts": ROOT_TS, "text": "[시작] 작업"}],
        "response_metadata": {"next_cursor": ""},
    }
    lock = Mock()

    with patch(
        "service.slack_task_thread.acquire_task_record_lock", return_value=lock
    ), patch("service.slack_task_thread.release_task_record_lock") as release:
        result = json.loads(
            await start_task_from_slack_list(
                client, LIST_URL, "owner@example.com", "Codex"
            )
        )

    assert client.chat_postMessage.await_count == 2
    root_call, reply_call = client.chat_postMessage.await_args_list
    assert root_call.kwargs == {
        "channel": CHANNEL,
        "text": f"<{LIST_URL}|교육생 계정 일괄 생성>",
    }
    assert reply_call.kwargs == {
        "channel": CHANNEL,
        "thread_ts": ROOT_TS,
        "text": (
            "• 시작한 사람: owner@example.com\n"
            "• 시작 시각: "
            "<!date^1700000000^{date_short_pretty} {time}|1700000000.000100>\n"
            "• 사용 도구: Codex"
        ),
    }
    client.slackLists_items_update.assert_awaited_once_with(
        list_id=LIST_ID,
        cells=[
            {
                "row_id": RECORD_ID,
                "column_id": "ColWork",
                "message": [ROOT_URL],
            }
        ],
    )
    release.assert_called_once_with(lock)
    assert result["work_thread_created"] is True
    assert len(result["source_threads"]) == 1
    assert result["work_thread"]["messages"][0]["text"] == "[시작] 작업"


async def test_start_reuses_existing_thread_without_posting():
    client = AsyncMock()
    work_ref = {"value": ROOT_URL, "channel_id": CHANNEL, "ts": ROOT_TS}
    client.slackLists_items_info.return_value = info(
        record(source=[SOURCE_REF], work=work_ref)
    )
    client.conversations_replies.side_effect = [
        {"messages": [{"user": "U01", "ts": "1699999999.000000", "text": "요청 배경"}]},
        {"messages": [{"bot_id": "B01", "ts": ROOT_TS, "text": "[시작]"}]},
    ]
    client.chat_getPermalink.side_effect = [
        {"permalink": SOURCE_URL},
        {"permalink": ROOT_URL},
    ]

    with patch("service.slack_task_thread.acquire_task_record_lock") as acquire:
        result = json.loads(
            await start_task_from_slack_list(
                client, LIST_URL, "owner@example.com", "Codex"
            )
        )

    acquire.assert_not_called()
    client.chat_postMessage.assert_not_awaited()
    client.slackLists_items_update.assert_not_awaited()
    assert result["work_thread_created"] is False
    assert result["source_threads"][0]["messages"][0]["text"] == "요청 배경"


async def test_start_rechecks_record_after_lock_before_creating_root():
    client = AsyncMock()
    work_ref = {"value": ROOT_URL, "channel_id": CHANNEL, "ts": ROOT_TS}
    client.slackLists_items_info.side_effect = [
        info(record()),
        info(record(work=work_ref)),
    ]
    client.conversations_replies.return_value = {
        "messages": [{"bot_id": "B01", "ts": ROOT_TS, "text": "[시작]"}]
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}
    lock = Mock()

    with patch(
        "service.slack_task_thread.acquire_task_record_lock", return_value=lock
    ), patch("service.slack_task_thread.release_task_record_lock") as release:
        result = json.loads(
            await start_task_from_slack_list(
                client, LIST_URL, "owner@example.com", "Codex"
            )
        )

    client.chat_postMessage.assert_not_awaited()
    client.slackLists_items_update.assert_not_awaited()
    release.assert_called_once_with(lock)
    assert result["work_thread_created"] is False


@pytest.mark.parametrize(
    "source",
    [None, {"value": "not-a-slack-link"}],
)
async def test_start_requires_valid_source_when_creating_work_thread(source):
    client = AsyncMock()
    client.slackLists_items_info.return_value = info(record(source=source))
    lock = Mock()

    with patch(
        "service.slack_task_thread.acquire_task_record_lock", return_value=lock
    ), patch("service.slack_task_thread.release_task_record_lock") as release:
        with pytest.raises(ValueError, match="요청 맥락"):
            await start_task_from_slack_list(
                client, LIST_URL, "owner@example.com", "Codex"
            )

    client.chat_postMessage.assert_not_awaited()
    release.assert_called_once_with(lock)


async def test_start_reports_broken_source_when_work_thread_already_exists():
    client = AsyncMock()
    work_ref = {"value": ROOT_URL, "channel_id": CHANNEL, "ts": ROOT_TS}
    client.slackLists_items_info.return_value = info(
        record(source={"value": "not-a-slack-link"}, work=work_ref)
    )
    client.conversations_replies.return_value = {
        "messages": [{"bot_id": "B01", "ts": ROOT_TS, "text": "[시작]"}]
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}

    result = json.loads(
        await start_task_from_slack_list(client, LIST_URL, "owner@example.com", "Codex")
    )

    assert result["work_thread_created"] is False
    assert result["source_threads"][0]["messages"] == []
    assert "읽지 못했습니다" in result["source_threads"][0]["error"]
    assert result["work_thread"]["messages"][0]["text"] == "[시작]"


async def test_publish_posts_curated_learnings_and_marks_completed():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info(
        record(work={"channel_id": CHANNEL, "ts": ROOT_TS})
    )
    client.chat_postMessage.return_value = {
        "channel": CHANNEL,
        "ts": "1700000004.000200",
    }
    result_url = (
        "https://example.slack.com/archives/C0ABCDE1234/"
        "p1700000004000200?thread_ts=1700000000.000100&cid=C0ABCDE1234"
    )
    client.chat_getPermalink.return_value = {"permalink": result_url}

    result = json.loads(
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "계정 68개를 생성하고 로그인을 확인했습니다.",
            learnings=[
                "전체 명단을 한 번에 처리하니 승인 대상이 섞여, 승인 여부로 먼저 나눴습니다."
            ],
            reusable_findings=["외부 강사는 별도 승인이 필요합니다."],
            outputs=["https://docs.example.com/account-result"],
            validation=["생성 수와 샘플 로그인을 확인했습니다."],
            remaining=["외부 강사 12명 승인 대기"],
            mark_completed=True,
        )
    )

    sent = client.chat_postMessage.await_args.kwargs
    assert sent["thread_ts"] == ROOT_TS
    assert "시행착오·경험:" in sent["text"]
    assert "승인 여부로 먼저 나눴습니다" in sent["text"]
    assert "일반 탐색" not in sent["text"]
    client.slackLists_items_update.assert_awaited_once_with(
        list_id=LIST_ID,
        cells=[{"row_id": RECORD_ID, "column_id": "ColDone", "checkbox": [True]}],
    )
    assert result["permalink"] == result_url
    assert result["list_marked_completed"] is True


async def test_publish_rejects_too_many_learnings_before_slack_call():
    client = AsyncMock()

    with pytest.raises(ValueError, match="최대 3개"):
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "충분히 구체적인 완료 요약입니다.",
            learnings=["하나", "둘", "셋", "넷"],
        )

    client.chat_postMessage.assert_not_awaited()


async def test_publish_rejects_secrets_and_local_paths():
    client = AsyncMock()

    with pytest.raises(ValueError, match="비밀값"):
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "토큰 xoxb-abcdefghijklmnopqrstuvwxyz 를 사용했습니다.",
        )

    with pytest.raises(ValueError, match="로컬 절대경로"):
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "결과는 /Users/name/report.md 에 저장했습니다.",
        )
