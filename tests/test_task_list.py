"""
채널 작업 리스트 도구 테스트

슬랙도 DB도 부르지 않는 순수 함수만 본다. 셀 조립이 틀리면 슬랙이
invalid_arguments 로 거절하는데, 그 실패는 운영에서만 드러난다.
"""

from unittest.mock import AsyncMock

from app.task_list import (
    _list_all_items,
    build_initial_fields,
    build_title_cell,
    is_completed,
    read_cell,
)
from service.slack_task_list import (
    ChannelTaskList,
    build_list_name,
    columns_from_schema,
    to_task_list,
)

TASK_LIST = ChannelTaskList(
    list_id="F09ABCDEFGH",
    url="https://example.slack.com/lists/T1/F09ABCDEFGH",
    name_column_id="Col012A3BCDE4",
    completed_column_id="Col00",
    assignee_column_id="Col01",
    due_date_column_id="Col02",
)
THREAD_URL = "https://example.slack.com/archives/C0XXXX/p1700000000"
REQUESTER = "U02JLCWGETT"

CREATE_SCHEMA = [
    {
        "key": "name",
        "name": "Name",
        "is_primary_column": True,
        "type": "text",
        "id": "Col012A3BCDE4",
    },
    {"key": "todo_completed", "type": "todo_completed", "id": "Col00"},
    {"key": "todo_assignee", "type": "todo_assignee", "id": "Col01"},
    {"key": "todo_due_date", "type": "todo_due_date", "id": "Col02"},
]


def test_list_name_takes_last_segment():
    """리스트 이름은 채널 이름의 마지막 조각에서 딴다"""
    assert build_list_name("t_고객_사업운영_OO교육청") == "OO교육청 작업"
    assert build_list_name("general") == "general 작업"


def test_columns_from_schema_normalizes_primary_column():
    """제목 열은 is_primary_column 으로 찾아 name 으로 통일한다"""
    columns = columns_from_schema(CREATE_SCHEMA)

    assert columns["name"] == "Col012A3BCDE4"
    assert columns["todo_completed"] == "Col00"
    assert columns["todo_assignee"] == "Col01"
    assert columns["todo_due_date"] == "Col02"


def test_to_task_list_maps_every_column():
    """생성 응답의 열 매핑이 그대로 ChannelTaskList 가 된다"""
    task_list = to_task_list(
        "F09ABCDEFGH",
        "https://example.slack.com/lists/T1/F09ABCDEFGH",
        columns_from_schema(CREATE_SCHEMA),
    )

    assert task_list == TASK_LIST


def test_title_cell_carries_thread_link():
    """제목 셀에 제목과 스레드 링크가 함께 들어간다"""
    cell = build_title_cell("Col012A3BCDE4", "계정 일괄 생성", THREAD_URL)

    assert cell["column_id"] == "Col012A3BCDE4"
    elements = cell["rich_text"][0]["elements"][0]["elements"]
    assert elements[0]["text"] == "계정 일괄 생성 "
    assert elements[1]["url"] == THREAD_URL


def test_requester_becomes_default_assignee():
    """담당자를 지정하지 않으면 요청한 사람이 담당자가 된다"""
    fields = build_initial_fields({"title": "작업"}, TASK_LIST, REQUESTER, THREAD_URL)

    assert {"column_id": "Col01", "user": [REQUESTER]} in fields


def test_explicit_assignee_wins():
    """담당자를 지정하면 요청자 대신 그 사람이 들어간다"""
    fields = build_initial_fields(
        {"title": "작업", "assignee": "U075PUFNGHX"},
        TASK_LIST,
        REQUESTER,
        THREAD_URL,
    )

    assert {"column_id": "Col01", "user": ["U075PUFNGHX"]} in fields


def test_assignee_cell_omitted_without_requester():
    """요청자도 담당자도 없으면 담당자 셀을 보내지 않는다"""
    fields = build_initial_fields({"title": "작업"}, TASK_LIST, None, THREAD_URL)

    assert all(field["column_id"] != "Col01" for field in fields)


def test_due_date_cell_omitted_when_absent():
    """마감일이 없으면 날짜 셀을 아예 보내지 않는다"""
    fields = build_initial_fields({"title": "작업"}, TASK_LIST, REQUESTER, THREAD_URL)

    assert all(field["column_id"] != "Col02" for field in fields)


def test_due_date_cell_sent_when_present():
    """마감일이 있으면 날짜 셀을 배열로 보낸다"""
    fields = build_initial_fields(
        {"title": "작업", "due_date": "2026-09-02"},
        TASK_LIST,
        REQUESTER,
        THREAD_URL,
    )

    assert {"column_id": "Col02", "date": ["2026-09-02"]} in fields


def test_read_cell_picks_matching_column():
    """항목에서 열 ID로 값을 찾아 읽는다"""
    item = {
        "id": "Rec01",
        "fields": [
            {"column_id": "Col012A3BCDE4", "text": "계정 일괄 생성 ↗ 슬랙"},
            {"column_id": "Col00", "checkbox": [True]},
        ],
    }

    assert read_cell(item, "Col012A3BCDE4", "text") == "계정 일괄 생성 ↗ 슬랙"
    assert read_cell(item, "Col00", "checkbox") == [True]
    assert read_cell(item, "Col99", "text") is None


def test_unchecked_item_is_not_completed():
    """완료 칸은 배열로 온다. 체크를 푼 [False] 를 완료로 읽으면 안 된다"""

    def item(checkbox):
        return {"id": "Rec01", "fields": [{"column_id": "Col00", "checkbox": checkbox}]}

    assert is_completed(item([True]), "Col00") is True
    assert is_completed(item([False]), "Col00") is False
    assert is_completed(item([]), "Col00") is False
    assert is_completed({"id": "Rec01", "fields": []}, "Col00") is False


async def test_list_all_items_follows_cursor():
    """완료 항목까지 함께 오므로 커서를 끝까지 따라간다"""
    client = AsyncMock()
    client.slackLists_items_list.side_effect = [
        {"items": [{"id": "Rec01"}], "response_metadata": {"next_cursor": "c1"}},
        {"items": [{"id": "Rec02"}], "response_metadata": {"next_cursor": ""}},
    ]

    items = await _list_all_items(client, "F09ABCDEFGH")

    assert [item["id"] for item in items] == ["Rec01", "Rec02"]
    assert client.slackLists_items_list.await_count == 2
