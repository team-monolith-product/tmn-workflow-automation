"""운영 포스트모템 게시와 개선 작업 연결 테스트입니다."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from service.operational_postmortem import publish_operational_postmortem

LIST_ID = "F0BTVRW2AAU"
RECORD_ID = "Rec0BSXEKFH1T"
IMPROVEMENT_RECORD_ID = "Rec0IMPROVE01"
CHANNEL = "C0ABCDE1234"
LIST_URL = (
    "https://monolith-keb2010.slack.com/lists/T02F55Y6M4M/"
    f"{LIST_ID}?record_id={RECORD_ID}"
)
SOURCE_TS = "1699999999.000000"
WORK_TS = "1700000000.000100"
POST_TS = "1700000004.000200"
SOURCE_URL = f"https://example.slack.com/archives/{CHANNEL}/p1699999999000000"
WORK_URL = f"https://example.slack.com/archives/{CHANNEL}/p1700000000000100"
POST_URL = (
    f"https://example.slack.com/archives/{CHANNEL}/p1700000004000200"
    f"?thread_ts={SOURCE_TS}&cid={CHANNEL}"
)

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


def record() -> dict:
    return {
        "id": RECORD_ID,
        "fields": [
            {"column_id": "ColTitle", "text": "3차 연수 출결 관리"},
            {"column_id": "ColDone", "checkbox": [False]},
            {"column_id": "ColSource", "message": [SOURCE_URL]},
            {"column_id": "ColWork", "message": [WORK_URL]},
        ],
    }


def info() -> dict:
    return {
        "list": {"list_metadata": {"schema": SCHEMA}},
        "record": record(),
    }


def postmortem_args() -> dict:
    return {
        "actor": "owner@example.com",
        "list_url": LIST_URL,
        "incident_key": "attendance-context-and-readiness",
        "target_thread_url": SOURCE_URL,
        "title": "과거 운영 맥락을 회수하지 못한 채 출결 작업을 시작함",
        "expected": "과거 기수 방식과 현재 운영 확정 여부를 확인한 뒤 필요한 작업만 시작한다.",
        "actual": "수기 서명부 기록을 찾지 못했고 신청자도 없는 상태에서 QR 출결 양식을 만들었다.",
        "confirmed_causes": ["현재 작업 생성 맥락에 과거 기수 자료가 연결되지 않았다."],
        "hypotheses": ["출석부·입퇴실 서명부 등 동의어 검색이 빠졌을 수 있다."],
        "missed_signals": ["신청자와 운영 방식이 아직 확정되지 않았다."],
        "investigation_items": [
            "과거 실행에서 사용한 검색어와 첨부파일 수집 범위를 확인한다."
        ],
        "system_changes": ["작업 시작 전에 동의어 검색과 착수 조건 확인을 강제한다."],
        "improvement_task_title": "운영 작업 검색·착수 조건 게이트 개선",
        "improvement_target": "tmn-operating의 start-operate-task와 Knowledge 검색 범위",
        "completion_criteria": [
            "동의어 검색과 선행 조건 대기 규칙이 스킬과 테스트에 반영된다."
        ],
        "references": [f"원 요청 스레드: {SOURCE_URL}"],
    }


async def test_postmortem_stays_in_cause_thread_and_creates_pending_task():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info()
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
    client.auth_test.return_value = {"user_id": "U0AUTOMATION"}
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": POST_TS}
    client.chat_getPermalink.return_value = {"permalink": POST_URL}
    client.slackLists_items_list.return_value = {
        "items": [],
        "response_metadata": {"next_cursor": ""},
    }
    client.slackLists_items_create.return_value = {
        "item": {"id": IMPROVEMENT_RECORD_ID}
    }
    lock = Mock()

    with patch(
        "service.operational_postmortem.acquire_task_record_lock",
        return_value=lock,
    ), patch("service.operational_postmortem.release_task_record_lock") as release:
        result = json.loads(
            await publish_operational_postmortem(client=client, **postmortem_args())
        )

    post = client.chat_postMessage.await_args.kwargs
    assert post["thread_ts"] == SOURCE_TS
    assert post["text"].startswith("<@U0AUTOMATION> [운영 포스트모템]")
    assert "확인된 원인:" in post["text"]
    assert "아직 조사할 원인:" in post["text"]
    assert post["text"].rfind("참고 자료:") > post["text"].rfind("완료 기준:")
    assert "client_msg_id" in post

    create = client.slackLists_items_create.await_args.kwargs
    assert create["list_id"] == LIST_ID
    assert {"column_id": "ColSource", "message": [POST_URL]} in create["initial_fields"]
    assert {"column_id": "ColWork", "message": [POST_URL]} in create["initial_fields"]
    assert {"column_id": "ColOwner", "user": ["U01OWNER"]} in create["initial_fields"]
    assert all(field["column_id"] != "ColDone" for field in create["initial_fields"])
    assert result["improvement_task"]["created"] is True
    assert result["improvement_task"]["status"] == "pending"
    assert (
        f"record_id={IMPROVEMENT_RECORD_ID}" in result["improvement_task"]["list_url"]
    )
    release.assert_called_once_with(lock)


async def test_postmortem_without_followup_does_not_tag_or_create_task():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info()
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": POST_TS}
    client.chat_getPermalink.return_value = {"permalink": POST_URL}
    args = postmortem_args()
    for name in (
        "improvement_task_title",
        "improvement_target",
        "completion_criteria",
    ):
        args.pop(name)
    lock = Mock()

    with patch(
        "service.operational_postmortem.acquire_task_record_lock",
        return_value=lock,
    ), patch("service.operational_postmortem.release_task_record_lock"):
        result = json.loads(await publish_operational_postmortem(client=client, **args))

    assert "<@" not in client.chat_postMessage.await_args.kwargs["text"]
    client.auth_test.assert_not_awaited()
    client.slackLists_items_create.assert_not_awaited()
    assert result["improvement_task"] is None


async def test_postmortem_links_other_context_without_copying_the_report():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info()
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
    client.auth_test.return_value = {"user_id": "U0AUTOMATION"}
    client.chat_postMessage.side_effect = [
        {"channel": CHANNEL, "ts": POST_TS},
        {"channel": CHANNEL, "ts": "1700000005.000300"},
    ]
    client.chat_getPermalink.return_value = {"permalink": POST_URL}
    client.slackLists_items_list.return_value = {
        "items": [],
        "response_metadata": {"next_cursor": ""},
    }
    client.slackLists_items_create.return_value = {
        "item": {"id": IMPROVEMENT_RECORD_ID}
    }
    args = postmortem_args() | {"related_thread_url": WORK_URL}

    with patch(
        "service.operational_postmortem.acquire_task_record_lock",
        return_value=Mock(),
    ), patch("service.operational_postmortem.release_task_record_lock"):
        result = json.loads(await publish_operational_postmortem(client=client, **args))

    original, related = client.chat_postMessage.await_args_list
    assert original.kwargs["thread_ts"] == SOURCE_TS
    assert related.kwargs["thread_ts"] == WORK_TS
    assert "실패 원인과 개선 작업" in related.kwargs["text"]
    assert "확인된 원인:" not in related.kwargs["text"]
    assert result["related_link_posted"] is True


async def test_postmortem_retry_reuses_task_linked_to_same_message():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info()
    client.users_lookupByEmail.return_value = {"user": {"id": "U01OWNER"}}
    client.auth_test.return_value = {"user_id": "U0AUTOMATION"}
    client.chat_postMessage.return_value = {"channel": CHANNEL, "ts": POST_TS}
    client.chat_getPermalink.return_value = {"permalink": POST_URL}
    client.slackLists_items_list.return_value = {
        "items": [
            {
                "id": IMPROVEMENT_RECORD_ID,
                "fields": [
                    {"column_id": "ColSource", "message": [POST_URL]},
                    {"column_id": "ColWork", "message": [POST_URL]},
                ],
            }
        ],
        "response_metadata": {"next_cursor": ""},
    }

    with patch(
        "service.operational_postmortem.acquire_task_record_lock",
        return_value=Mock(),
    ), patch("service.operational_postmortem.release_task_record_lock"):
        result = json.loads(
            await publish_operational_postmortem(client=client, **postmortem_args())
        )

    client.slackLists_items_create.assert_not_awaited()
    assert result["improvement_task"]["created"] is False
    assert result["improvement_task"]["record_id"] == IMPROVEMENT_RECORD_ID


async def test_postmortem_rejects_unlinked_target_and_unsupported_conclusion():
    client = AsyncMock()
    client.slackLists_items_info.return_value = info()
    args = postmortem_args() | {
        "target_thread_url": "https://example.slack.com/archives/C0OTHER/p1700000000000000"
    }

    with patch(
        "service.operational_postmortem.acquire_task_record_lock",
        return_value=Mock(),
    ), patch("service.operational_postmortem.release_task_record_lock"):
        with pytest.raises(ValueError, match="요청 맥락 또는 작업 기록"):
            await publish_operational_postmortem(client=client, **args)

    no_cause = postmortem_args() | {"confirmed_causes": [], "hypotheses": []}
    with pytest.raises(ValueError, match="원인"):
        await publish_operational_postmortem(client=client, **no_cause)
