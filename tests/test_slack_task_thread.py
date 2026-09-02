"""Slack List 작업 시작과 종료 요약 테스트."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from slack_sdk.errors import SlackApiError, SlackRequestError

from service.slack_task_message import result_client_msg_id
from service.slack_task_thread import (
    _actor_mention,
    find_work_thread_column_id,
    message_location,
    parse_slack_list_task_url,
    publish_task_result,
    record_task_references,
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


def publishing_client() -> AsyncMock:
    client = AsyncMock()
    client.conversations_replies.return_value = {
        "messages": [],
        "response_metadata": {"next_cursor": ""},
    }
    return client


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


def test_is_completed_reads_bare_bool_checkbox():
    """슬랙이 완료 칸을 배열이 아닌 단일 bool 로 돌려줄 때도 있다 (WORKFLOW-AUTOMATION-73/74)"""
    schema = task_list_schema(SCHEMA)
    item = {"id": RECORD_ID, "fields": [{"column_id": "ColDone", "checkbox": True}]}

    assert schema.is_completed(item) is True
    assert (
        schema.is_completed(
            {**item, "fields": [{"column_id": "ColDone", "checkbox": False}]}
        )
        is False
    )


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


async def test_start_actor_falls_back_to_email_when_slack_user_is_not_found():
    client = AsyncMock()
    client.users_lookupByEmail.side_effect = SlackApiError(
        "users_not_found", {"error": "users_not_found"}
    )

    assert await _actor_mention(client, "owner@example.com") == "owner@example.com"


async def test_start_creates_one_root_and_stores_its_permalink():
    client = AsyncMock()
    client.slackLists_items_info.side_effect = [
        info(record(source=SOURCE_REF)),
        info(record(source=SOURCE_REF)),
    ]
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": ROOT_TS}
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
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
            await start_task_from_slack_list(client, LIST_URL, "owner@example.com")
        )

    assert client.chat_postMessage.await_count == 2
    root_call, reply_call = client.chat_postMessage.await_args_list
    assert root_call.kwargs == {
        "channel": CHANNEL,
        "text": ("작업을 시작합니다.\n" f"• 작업: <{LIST_URL}|교육생 계정 일괄 생성>"),
    }
    assert reply_call.kwargs == {
        "channel": CHANNEL,
        "thread_ts": ROOT_TS,
        "text": (
            "• 시작한 사람: <@U01OWNER>\n"
            "• 시작 시각: "
            "<!date^1700000000^{date_short_pretty} {time}|1700000000.000100>"
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
    assert result["execution_requirements"] == {
        "knowledge_query_before_work": True,
        "knowledge_tool": "query_knowledge",
        "knowledge_search_angles": [
            "같은 사업과 동일 업무",
            "유사 사업과 업무 패턴",
            "관련 요구사항, 결정, 실패 사례",
        ],
        "broaden_search_until_results_repeat": True,
        "repeat_search_on_material_change": True,
        "material_change_triggers": [
            "추가 요구사항 또는 예외",
            "범위, 방향, 대상, 산출물, 일정 변경",
            "새로운 고유 명사, 자료, 제약",
            "기존 판단과 충돌하는 정보",
        ],
        "coverage_goal": "서로 다른 관련 출처에서 새로운 사실이나 판단이 더 나오지 않을 때까지",
        "record_references_after_each_gate": True,
        "reference_tool": "record_slack_task_references",
        "ask_starter_if_no_references": True,
        "clarify_ambiguity_before_work": True,
        "runtime_metadata_at_finish": [
            "tool",
            "model",
            "reasoning_effort",
            "token_usage",
            "elapsed_time",
            "conversation_turns",
        ],
        "runtime_metadata_only_if_verified": True,
        "starter": "owner@example.com",
    }


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
            await start_task_from_slack_list(client, LIST_URL, "owner@example.com")
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
            await start_task_from_slack_list(client, LIST_URL, "owner@example.com")
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
    ), patch(
        "service.slack_task_thread.find_task_list_channel_id", return_value=None
    ), patch(
        "service.slack_task_thread.release_task_record_lock"
    ) as release:
        with pytest.raises(ValueError, match="요청 맥락"):
            await start_task_from_slack_list(client, LIST_URL, "owner@example.com")

    client.chat_postMessage.assert_not_awaited()
    release.assert_called_once_with(lock)


async def test_start_uses_channel_registered_to_list_without_source():
    client = AsyncMock()
    client.slackLists_items_info.side_effect = [info(record()), info(record())]
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": ROOT_TS}
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}
    client.conversations_replies.return_value = {
        "messages": [{"bot_id": "B01", "ts": ROOT_TS, "text": "[시작]"}]
    }
    lock = Mock()

    with patch(
        "service.slack_task_thread.acquire_task_record_lock", return_value=lock
    ), patch(
        "service.slack_task_thread.find_task_list_channel_id", return_value=CHANNEL
    ) as find_channel, patch(
        "service.slack_task_thread.release_task_record_lock"
    ):
        result = json.loads(
            await start_task_from_slack_list(client, LIST_URL, "owner@example.com")
        )

    find_channel.assert_called_once_with(LIST_ID)
    assert client.chat_postMessage.await_args_list[0].kwargs["channel"] == CHANNEL
    assert result["source_threads"] == []
    assert result["work_thread_created"] is True


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
        await start_task_from_slack_list(client, LIST_URL, "owner@example.com")
    )

    assert result["work_thread_created"] is False
    assert result["source_threads"][0]["messages"] == []
    assert "읽지 못했습니다" in result["source_threads"][0]["error"]
    assert result["work_thread"]["messages"][0]["text"] == "[시작]"


async def test_publish_posts_curated_learnings_and_marks_completed():
    client = publishing_client()
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

    with patch("service.slack_task_thread.time.time", return_value=1700003600.0001):
        result = json.loads(
            await publish_task_result(
                client,
                LIST_URL,
                "owner@example.com",
                "completed",
                "계정 68개를 생성하고 로그인을 확인했습니다.",
                outputs=["계정 생성 결과: https://docs.example.com/account-result"],
                learnings=[
                    "전체 명단을 한 번에 처리하니 승인 대상이 섞여, 승인 여부로 먼저 나눴습니다."
                ],
                reusable_findings=["외부 강사는 별도 승인이 필요합니다."],
                validation=["생성 수와 샘플 로그인을 확인했습니다."],
                remaining=["외부 강사 12명 승인 대기"],
                references=[
                    "이전 계정 생성 작업: https://example.slack.com/archives/C01/p1"
                ],
                client_name="Codex",
                model="gpt-5.6-sol",
                reasoning_effort="high",
                input_tokens=12_000,
                cached_input_tokens=8_000,
                output_tokens=3_500,
                reasoning_output_tokens=1_200,
                conversation_turns=7,
            )
        )

    sent = client.chat_postMessage.await_args.kwargs
    assert sent["thread_ts"] == ROOT_TS
    assert sent["client_msg_id"] == result_client_msg_id(LIST_ID, RECORD_ID)
    assert "시행착오·경험:" in sent["text"]
    assert "승인 여부로 먼저 나눴습니다" in sent["text"]
    assert "일반 탐색" not in sent["text"]
    assert "상태:" not in sent["text"]
    assert (
        "산출물: <https://docs.example.com/account-result|계정 생성 결과>"
        in sent["text"]
    )
    assert "• 도구: Codex" in sent["text"]
    assert "• 모델: gpt-5.6-sol" in sent["text"]
    assert "• Effort: high" in sent["text"]
    assert "• 토큰: 총 15,500" in sent["text"]
    assert "• 전체 시간: 1시간" in sent["text"]
    assert "• 대화 턴: 7" in sent["text"]
    assert (
        "<https://example.slack.com/archives/C01/p1|이전 계정 생성 작업>"
        in sent["text"]
    )
    assert "실행 메타 정보:" not in sent["blocks"][0]["text"]["text"]
    assert "실행 메타 정보:" in sent["blocks"][-1]["text"]["text"]
    assert "참고 자료 (1건):" in sent["blocks"][-1]["text"]["text"]
    assert sent["blocks"][-1]["expand"] is False
    client.slackLists_items_update.assert_awaited_once_with(
        list_id=LIST_ID,
        cells=[{"row_id": RECORD_ID, "column_id": "ColDone", "checkbox": True}],
    )
    client.reactions_add.assert_awaited_once_with(
        channel=CHANNEL,
        timestamp=ROOT_TS,
        name="white_check_mark",
    )
    assert result["permalink"] == result_url
    assert result["outcome"] == "success"
    assert result["message_action"] == "created"
    assert result["retry_post"] is False
    assert result["list_marked_completed"] is True
    assert result["completion_reaction_added"] is True


async def test_publish_updates_existing_result_in_work_thread():
    client = publishing_client()
    client.slackLists_items_info.return_value = info(
        record(work={"channel_id": CHANNEL, "ts": ROOT_TS})
    )
    message_ts = "1700000004.000200"
    client.conversations_replies.return_value = {
        "messages": [
            {
                "ts": message_ts,
                "client_msg_id": result_client_msg_id(LIST_ID, RECORD_ID),
            }
        ],
        "response_metadata": {"next_cursor": ""},
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}

    result = json.loads(
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "기존 결과 메시지를 최신 내용으로 갱신했습니다.",
            outputs=[],
        )
    )

    client.chat_postMessage.assert_not_awaited()
    client.chat_update.assert_awaited_once()
    assert client.chat_update.await_args.kwargs["ts"] == message_ts
    assert result["message_action"] == "updated"


async def test_record_references_posts_one_curated_gate_log():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info(
        record(work={"channel_id": CHANNEL, "ts": ROOT_TS})
    )
    client.chat_postMessage.return_value = {
        "channel": CHANNEL,
        "ts": "1700000004.000200",
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}

    result = json.loads(
        await record_task_references(
            client,
            LIST_URL,
            "요구사항 변경: 공유 대상 추가",
            [
                "기존 컨소시엄 공유 사례: https://example.slack.com/archives/C01/p1",
                "기존 컨소시엄 공유 사례 중복: https://example.slack.com/archives/C01/p1",
                "운영 가이드: https://docs.example.com/guide",
            ],
        )
    )

    sent = client.chat_postMessage.await_args.kwargs
    assert sent["thread_ts"] == ROOT_TS
    assert "[참고 자료] 요구사항 변경: 공유 대상 추가" in sent["text"]
    assert sent["text"].count("https://example.slack.com/archives/C01/p1") == 1
    assert "<https://docs.example.com/guide|운영 가이드>" in sent["text"]
    assert all(block["expand"] is False for block in sent["blocks"])
    assert result["reference_count"] == 2


async def test_publish_keeps_blocked_task_open_by_default():
    client = publishing_client()
    client.slackLists_items_info.return_value = info(
        record(work={"channel_id": CHANNEL, "ts": ROOT_TS})
    )
    client.chat_postMessage.return_value = {
        "channel": CHANNEL,
        "ts": "1700000004.000200",
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}

    result = json.loads(
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "blocked",
            "외부 승인 확인이 없어 계정 생성을 진행하지 못했습니다.",
            outputs=[],
        )
    )

    client.slackLists_items_update.assert_not_awaited()
    client.reactions_add.assert_not_awaited()
    assert result["list_marked_completed"] is False
    assert result["completion_reaction_added"] is False


@pytest.mark.parametrize(
    "reaction_error",
    [
        SlackApiError("missing_scope", {"error": "missing_scope"}),
        SlackRequestError("connection reset"),
    ],
)
async def test_publish_keeps_completed_result_when_check_reaction_fails(
    reaction_error,
):
    client = publishing_client()
    client.slackLists_items_info.return_value = info(
        record(work={"channel_id": CHANNEL, "ts": ROOT_TS})
    )
    client.chat_postMessage.return_value = {
        "channel": CHANNEL,
        "ts": "1700000004.000200",
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}
    client.reactions_add.side_effect = reaction_error

    result = json.loads(
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "계정 생성 결과를 확인해 완료 처리했습니다.",
            outputs=[],
        )
    )

    client.slackLists_items_update.assert_awaited_once()
    assert result["list_marked_completed"] is True
    assert result["completion_reaction_added"] is False


async def test_publish_returns_partial_success_when_only_list_update_fails():
    client = publishing_client()
    client.slackLists_items_info.return_value = info(
        record(work={"channel_id": CHANNEL, "ts": ROOT_TS})
    )
    client.chat_postMessage.return_value = {
        "channel": CHANNEL,
        "ts": "1700000004.000200",
    }
    client.chat_getPermalink.return_value = {"permalink": ROOT_URL}
    client.slackLists_items_update.side_effect = SlackApiError(
        "invalid arguments", {"error": "invalid_arguments"}
    )

    result = json.loads(
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "결과 댓글은 게시됐지만 List 완료 갱신은 실패했습니다.",
            outputs=[],
        )
    )

    assert result == {
        "posted": True,
        "status": "completed",
        "outcome": "partial_success",
        "message_action": "created",
        "permalink": ROOT_URL,
        "list_marked_completed": False,
        "list_update_error": "invalid_arguments",
        "retry_post": False,
        "completion_reaction_added": False,
    }
    client.reactions_add.assert_not_awaited()


async def test_publish_rejects_too_many_learnings_before_slack_call():
    client = AsyncMock()

    with pytest.raises(ValueError, match="최대 3개"):
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "충분히 구체적인 완료 요약입니다.",
            outputs=[],
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
            outputs=[],
        )

    with pytest.raises(ValueError, match="로컬 절대경로"):
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "결과는 /Users/name/report.md 에 저장했습니다.",
            outputs=[],
        )


async def test_publish_rejects_output_without_shareable_link():
    client = AsyncMock()

    with pytest.raises(ValueError, match="산출물 이름"):
        await publish_task_result(
            client,
            LIST_URL,
            "owner@example.com",
            "completed",
            "확정된 연수 안내문을 작성했습니다.",
            outputs=["최종 연수 안내문"],
        )

    client.chat_postMessage.assert_not_awaited()
