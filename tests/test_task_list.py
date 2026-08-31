"""
채널 작업 리스트 테스트

셀 조립이 틀리면 슬랙이 invalid_arguments 로 거절하는데 그 실패는 운영에서만
드러난다. 슬랙을 부르는 자리는 AsyncMock 으로 대신하고 DB 는 부르지 않는다.
"""

from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from pydantic import ValidationError

from app import general
from app.task_list import TaskInput, default_due_date, get_task_list_write_tools
from service.slack_task_list import (
    ChannelTaskList,
    create_channel_task_list,
    list_all_items,
    to_task_list,
    validate_task_list_channel,
)

TASK_LIST = ChannelTaskList(
    list_id="F09ABCDEFGH",
    list_url="https://example.slack.com/lists/T1/F09ABCDEFGH",
    name_column_id="Col012A3BCDE4",
    completed_column_id="Col00",
    assignee_column_id="Col01",
    due_date_column_id="Col02",
    thread_column_id="Col05F6GHIJ7K",
)

# 슬랙 열이 생기기 전에 만들어진 리스트. 열을 나중에 붙이는 API 가 없어 이런
# 리스트는 계속 남는다.
LEGACY_TASK_LIST = ChannelTaskList(
    list_id=TASK_LIST.list_id,
    list_url=TASK_LIST.list_url,
    name_column_id=TASK_LIST.name_column_id,
    completed_column_id=TASK_LIST.completed_column_id,
    assignee_column_id=TASK_LIST.assignee_column_id,
    due_date_column_id=TASK_LIST.due_date_column_id,
)
THREAD_URL = "https://example.slack.com/archives/C0XXXX/p1700000000"
REQUESTER = "U02JLCWGETT"
CHANNEL = "C0XXXX"

CREATE_SCHEMA = [
    {
        "key": "name",
        "name": "Name",
        "is_primary_column": True,
        "type": "text",
        "id": "Col012A3BCDE4",
    },
    {
        "key": "slack_thread",
        "name": "요청 맥락",
        "type": "message",
        "id": "Col05F6GHIJ7K",
    },
    {
        "key": "work_thread",
        "name": "작업 기록",
        "type": "message",
        "id": "Col09WORK1234",
    },
    {"key": "todo_completed", "type": "todo_completed", "id": "Col00"},
    {"key": "todo_assignee", "type": "todo_assignee", "id": "Col01"},
    {"key": "todo_due_date", "type": "todo_due_date", "id": "Col02"},
]


def item(row_id: str, title: str | None = None, checkbox: list | None = None) -> dict:
    """slackLists.items.list 항목 하나를 만든다. 빈 셀은 fields 에서 빠진다"""
    fields = []
    if title is not None:
        fields.append({"column_id": "Col012A3BCDE4", "text": title})
    if checkbox is not None:
        fields.append({"column_id": "Col00", "checkbox": checkbox})
    return {"id": row_id, "fields": fields}


# --- 생성 응답 파싱 ---


def test_to_task_list_finds_primary_column():
    """제목 열은 is_primary_column 으로 찾고 할 일 열은 키로 찾는다"""
    task_list = to_task_list(TASK_LIST.list_id, TASK_LIST.list_url, CREATE_SCHEMA)

    assert task_list == TASK_LIST


# --- 채널 검사 ---


def test_external_shared_channel_is_rejected():
    """외부와 공유된 채널은 막는다. 리스트 쓰기 권한이 외부 조직에 넘어간다"""
    assert validate_task_list_channel({"is_ext_shared": True}) is not None
    assert validate_task_list_channel({"is_pending_ext_shared": True}) is not None
    assert validate_task_list_channel({"is_im": True}) is not None
    assert validate_task_list_channel({"is_mpim": True}) is not None
    assert validate_task_list_channel({"is_channel": True}) is None


# --- 셀 조립 ---


def test_thread_link_goes_to_its_own_column():
    """제목은 제목만 담고 스레드 링크는 슬랙 열로 간다"""
    fields = TASK_LIST.initial_fields("계정 일괄 생성", None, None, THREAD_URL)

    assert fields[0]["column_id"] == "Col012A3BCDE4"
    elements = fields[0]["rich_text"][0]["elements"][0]["elements"]
    assert elements == [{"type": "text", "text": "계정 일괄 생성"}]
    assert {"column_id": "Col05F6GHIJ7K", "message": [THREAD_URL]} in fields


def test_legacy_list_keeps_link_in_title():
    """슬랙 열이 없는 옛 리스트는 예전처럼 제목 칸에 링크를 붙인다"""
    fields = LEGACY_TASK_LIST.initial_fields("작업", None, None, THREAD_URL)

    elements = fields[0]["rich_text"][0]["elements"][0]["elements"]
    assert elements[0]["text"] == "작업 "
    assert elements[1]["url"] == THREAD_URL
    assert [field["column_id"] for field in fields] == ["Col012A3BCDE4"]


def test_assignee_and_due_date_cells_are_arrays():
    """담당자와 마감일은 배열로 보낸다"""
    fields = TASK_LIST.initial_fields("작업", REQUESTER, "2026-09-02", THREAD_URL)

    assert {"column_id": "Col01", "user": [REQUESTER]} in fields
    assert {"column_id": "Col02", "date": ["2026-09-02"]} in fields


def test_empty_cells_are_omitted():
    """담당자와 마감일이 없으면 그 셀은 아예 보내지 않는다"""
    fields = TASK_LIST.initial_fields("작업", None, None, THREAD_URL)

    assert [field["column_id"] for field in fields] == [
        "Col012A3BCDE4",
        "Col05F6GHIJ7K",
    ]


def test_completion_cells_carry_row_ids():
    """완료 셀은 행마다 row_id와 boolean 값을 쓴다"""
    cells = TASK_LIST.completion_cells(["Rec01", "Rec02"])

    assert cells == [
        {"row_id": "Rec01", "column_id": "Col00", "checkbox": True},
        {"row_id": "Rec02", "column_id": "Col00", "checkbox": True},
    ]


# --- 셀 읽기 ---


def test_unchecked_item_is_not_completed():
    """완료 칸은 배열로 온다. 체크를 푼 [False] 를 완료로 읽으면 안 된다"""
    assert TASK_LIST.is_completed(item("R", checkbox=[True])) is True
    assert TASK_LIST.is_completed(item("R", checkbox=[False])) is False
    assert TASK_LIST.is_completed(item("R", checkbox=[])) is False
    assert TASK_LIST.is_completed(item("R")) is False


def test_reading_item_without_fields_key():
    """셀이 하나도 없는 항목은 fields 자체가 없을 수 있다"""
    assert TASK_LIST.title_of({"id": "Rec01"}) == ""
    assert TASK_LIST.is_completed({"id": "Rec01"}) is False


# --- 도구 인자 계약 ---


def test_task_input_rejects_malformed_values():
    """슬랙이 거절할 값은 API 를 부르기 전에 걸린다"""
    assert TaskInput(title="  작업  ").title == "작업"

    with pytest.raises(ValidationError):
        TaskInput(title="   ")
    with pytest.raises(ValidationError):
        TaskInput(title="작업", assignee="@홍길동")
    with pytest.raises(ValidationError):
        TaskInput(title="작업", due_date="다음주 화요일")


# --- 페이징 ---


async def test_list_all_items_follows_cursor():
    """완료 항목까지 함께 오므로 커서를 끝까지 따라간다"""
    client = AsyncMock()
    client.slackLists_items_list.side_effect = [
        {"items": [item("Rec01")], "response_metadata": {"next_cursor": "c1"}},
        {"items": [item("Rec02")], "response_metadata": {"next_cursor": ""}},
    ]

    items, truncated = await list_all_items(client, TASK_LIST.list_id)

    assert [row["id"] for row in items] == ["Rec01", "Rec02"]
    assert truncated is False


async def test_list_all_items_reports_truncation():
    """커서가 끝나지 않으면 상한에서 멈추고 잘렸다고 알린다"""
    client = AsyncMock()
    client.slackLists_items_list.return_value = {
        "items": [item("Rec01")],
        "response_metadata": {"next_cursor": "endless"},
    }

    _, truncated = await list_all_items(client, TASK_LIST.list_id)

    assert truncated is True
    assert client.slackLists_items_list.await_count == 20


# --- 리스트 생성 ---


async def test_create_saves_before_sharing():
    """공유가 실패해도 표가 리스트를 알고 있어야 중복 생성이 없다"""
    client = AsyncMock()
    client.slackLists_create.return_value = {
        "list_id": TASK_LIST.list_id,
        "list_metadata": {"schema": CREATE_SCHEMA},
    }
    client.auth_test.return_value = {
        "url": "https://example.slack.com/",
        "team_id": "T1",
    }
    order: list[str] = []
    client.slackLists_access_set.side_effect = lambda **kwargs: order.append("share")

    with patch(
        "service.slack_task_list.save_channel_task_list",
        side_effect=lambda *args: order.append("save"),
    ):
        task_list = await create_channel_task_list(client, CHANNEL, "t_고객_OO교육청")

    assert order == ["save", "share"]
    assert client.bookmarks_add.await_count == 0
    assert task_list == TASK_LIST
    assert client.slackLists_create.await_args.kwargs["name"] == "t_고객_OO교육청 작업"


async def test_create_defines_slack_column_up_front():
    """열은 만들 때만 정할 수 있으므로 슬랙 열을 schema 로 함께 보낸다"""
    client = AsyncMock()
    client.slackLists_create.return_value = {
        "list_id": TASK_LIST.list_id,
        "list_metadata": {"schema": CREATE_SCHEMA},
    }
    client.auth_test.return_value = {
        "url": "https://example.slack.com/",
        "team_id": "T1",
    }

    with patch("service.slack_task_list.save_channel_task_list"):
        await create_channel_task_list(client, CHANNEL, "t_고객_OO교육청")

    sent = client.slackLists_create.await_args.kwargs
    assert sent["todo_mode"] is True
    assert [column["type"] for column in sent["schema"]] == [
        "text",
        "message",
        "message",
    ]


# --- 도구 동작 ---


def write_tools(client) -> dict:
    """등록된 채널의 도구를 이름으로 찾을 수 있게 만든다"""
    tools = get_task_list_write_tools(client, CHANNEL, TASK_LIST, REQUESTER, THREAD_URL)
    return {tool.name: tool for tool in tools}


async def test_add_tasks_fills_assignee_from_requester():
    """담당자를 비우면 요청한 사람이 담당자가 된다"""
    client = AsyncMock()
    tools = write_tools(client)

    result = await tools["add_channel_tasks"].ainvoke(
        {"tasks": [{"title": "계정 생성"}, {"title": "자료 정리"}]}
    )

    assert client.slackLists_items_create.await_count == 2
    fields = client.slackLists_items_create.await_args.kwargs["initial_fields"]
    assert {"column_id": "Col01", "user": [REQUESTER]} in fields
    assert "2개" in result


async def test_add_tasks_fills_due_date_when_missing():
    """마감을 못 읽었어도 마감일 칸을 비워 두지 않는다"""
    client = AsyncMock()
    tools = write_tools(client)

    await tools["add_channel_tasks"].ainvoke({"tasks": [{"title": "작업"}]})

    fields = client.slackLists_items_create.await_args.kwargs["initial_fields"]
    assert {"column_id": "Col02", "date": [default_due_date()]} in fields


async def test_add_tasks_keeps_given_due_date():
    """대화에서 읽은 마감일이 있으면 기본값이 덮지 않는다"""
    client = AsyncMock()
    tools = write_tools(client)

    await tools["add_channel_tasks"].ainvoke(
        {"tasks": [{"title": "작업", "due_date": "2026-09-02"}]}
    )

    fields = client.slackLists_items_create.await_args.kwargs["initial_fields"]
    assert {"column_id": "Col02", "date": ["2026-09-02"]} in fields


async def test_complete_matches_by_substring():
    """제목 일부로 찾아 한 번의 호출로 전부 완료 처리한다"""
    client = AsyncMock()
    client.slackLists_items_list.return_value = {
        "items": [
            item("Rec01", title="계정 일괄 생성 ↗ 슬랙"),
            item("Rec02", title="자료 정리 ↗ 슬랙"),
            item("Rec03", title="이미 끝난 일 ↗ 슬랙", checkbox=[True]),
        ],
        "response_metadata": {"next_cursor": ""},
    }
    tools = write_tools(client)

    result = await tools["complete_channel_tasks"].ainvoke({"titles": ["계정 일괄"]})

    client.slackLists_items_update.assert_awaited_once()
    cells = client.slackLists_items_update.await_args.kwargs["cells"]
    assert [cell["row_id"] for cell in cells] == ["Rec01"]
    assert "1개" in result


async def test_complete_returns_pending_when_nothing_matches():
    """못 찾으면 완료된 항목을 뺀 미완료 목록을 돌려준다"""
    client = AsyncMock()
    client.slackLists_items_list.return_value = {
        "items": [
            item("Rec01", title="계정 일괄 생성 ↗ 슬랙"),
            item("Rec02", title="이미 끝난 일 ↗ 슬랙", checkbox=[True]),
        ],
        "response_metadata": {"next_cursor": ""},
    }
    tools = write_tools(client)

    result = await tools["complete_channel_tasks"].ainvoke({"titles": ["없는 작업"]})

    client.slackLists_items_update.assert_not_awaited()
    assert "계정 일괄 생성" in result
    assert "이미 끝난 일" not in result


async def test_complete_rejects_blank_titles():
    """빈 제목은 미완료 전체를 완료 처리해 버리므로 먼저 막는다"""
    client = AsyncMock()
    tools = write_tools(client)

    result = await tools["complete_channel_tasks"].ainvoke({"titles": ["", "  "]})

    client.slackLists_items_list.assert_not_awaited()
    assert "제목을 알려주세요" in result


async def test_disable_returns_list_url_and_leaves_slack_alone():
    """해제는 표에서만 끊는다. 리스트도 상단 탭도 슬랙에 그대로 둔다"""
    client = AsyncMock()
    tools = write_tools(client)

    with patch(
        "app.task_list.delete_channel_task_list", return_value=TASK_LIST.list_url
    ):
        result = await tools["disable_channel_task_list"].ainvoke({})

    assert TASK_LIST.list_url in result
    assert client.bookmarks_list.await_count == 0
    assert client.bookmarks_remove.await_count == 0


# --- 도구 선택 분기 ---


NOTION_FACTORIES = (
    "get_create_notion_task_tool",
    "get_create_notion_follow_up_task_tool",
    "get_update_notion_task_deadline_tool",
    "get_update_notion_task_status_tool",
    "get_notion_page_tool",
)


async def build_tools_with(channel: str, **task_list_patch) -> set[str]:
    """노션 도구를 제 이름표로 대신 세우고 _build_tools 가 고른 도구를 모은다"""
    factories = {
        name: (lambda *args, name=name, **kwargs: name) for name in NOTION_FACTORIES
    }
    with patch.object(general, "find_channel_task_list", **task_list_patch):
        with patch.multiple(general, **factories):
            with patch.object(general, "_get_user_squad", AsyncMock(return_value=None)):
                tools = await general._build_tools(
                    AsyncMock(), REQUESTER, channel, "1700000000.000100"
                )
    return {getattr(tool, "name", tool) for tool in tools}


async def test_registered_channel_swaps_notion_task_tools_for_list_tools():
    """등록된 채널에서는 작업 생성 도구만 리스트 도구로 바뀌고 조회·수정은 남는다"""
    names = await build_tools_with(CHANNEL, return_value=TASK_LIST)

    assert {
        "add_channel_tasks",
        "complete_channel_tasks",
        "disable_channel_task_list",
    } <= names
    assert "get_create_notion_task_tool" not in names
    assert "get_create_notion_follow_up_task_tool" not in names
    assert "get_notion_page_tool" in names
    assert "get_update_notion_task_status_tool" in names
    assert "enable_channel_task_list" not in names


async def test_unregistered_channel_keeps_notion_and_offers_enable():
    """등록 안 된 채널은 노션 흐름 그대로에 등록 도구만 더한다"""
    names = await build_tools_with(CHANNEL, return_value=None)

    assert "get_create_notion_task_tool" in names
    assert "get_create_notion_follow_up_task_tool" in names
    assert "enable_channel_task_list" in names
    assert "add_channel_tasks" not in names


async def test_db_outage_falls_back_to_notion():
    """접속이 안 되면 노션으로 내려간다. 여기서 터지면 봇이 전부 무응답이 된다"""
    names = await build_tools_with(
        CHANNEL, side_effect=psycopg.OperationalError("connection refused")
    )

    assert "get_create_notion_task_tool" in names
    assert "add_channel_tasks" not in names
    # 등록된 채널을 미등록으로 보고 켜면 아무도 안 읽는 리스트가 하나 더 생긴다
    assert "enable_channel_task_list" not in names


async def test_missing_table_is_not_swallowed():
    """표가 없는 것은 마이그레이션 누락이라 그대로 터뜨린다"""
    with pytest.raises(psycopg.ProgrammingError):
        await build_tools_with(
            CHANNEL, side_effect=psycopg.ProgrammingError("relation does not exist")
        )
