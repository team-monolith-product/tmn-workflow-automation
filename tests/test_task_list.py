"""
채널 작업 리스트 테스트

셀 조립이 틀리면 슬랙이 invalid_arguments 로 거절하는데 그 실패는 운영에서만
드러난다. 슬랙을 부르는 자리는 AsyncMock 으로 대신하고 DB 는 부르지 않는다.
"""

from unittest.mock import AsyncMock, patch

from app import general
from app.task_list import TaskInput, get_task_list_write_tools
from service.slack_task_list import (
    ChannelTaskList,
    build_list_name,
    columns_from_schema,
    list_all_items,
    reject_task_list_channel,
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
CHANNEL = "C0XXXX"

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


def item(row_id: str, title: str | None = None, checkbox: list | None = None) -> dict:
    """slackLists.items.list 항목 하나를 만든다. 빈 셀은 fields 에서 빠진다"""
    fields = []
    if title is not None:
        fields.append({"column_id": "Col012A3BCDE4", "text": title})
    if checkbox is not None:
        fields.append({"column_id": "Col00", "checkbox": checkbox})
    return {"id": row_id, "fields": fields}


# --- 스키마 매핑 ---


def test_list_name_uses_channel_name():
    """리스트 이름은 채널 이름에서 딴다"""
    assert (
        build_list_name("t_고객_사업운영_OO교육청") == "t_고객_사업운영_OO교육청 작업"
    )


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
        TASK_LIST.list_id, TASK_LIST.url, columns_from_schema(CREATE_SCHEMA)
    )

    assert task_list == TASK_LIST


# --- 채널 검사 ---


def test_external_shared_channel_is_rejected():
    """외부와 공유된 채널은 막는다. 리스트 쓰기 권한이 외부 조직에 넘어간다"""
    assert reject_task_list_channel({"is_ext_shared": True}) is not None
    assert reject_task_list_channel({"is_pending_ext_shared": True}) is not None
    assert reject_task_list_channel({"is_im": True}) is not None
    assert reject_task_list_channel({"is_channel": True}) is None


# --- 셀 조립 ---


def test_title_cell_carries_thread_link():
    """제목 셀에 제목과 스레드 링크가 함께 들어간다"""
    fields = TASK_LIST.initial_fields("계정 일괄 생성", None, None, THREAD_URL)

    assert fields[0]["column_id"] == "Col012A3BCDE4"
    elements = fields[0]["rich_text"][0]["elements"][0]["elements"]
    assert elements[0]["text"] == "계정 일괄 생성 "
    assert elements[1]["url"] == THREAD_URL


def test_assignee_and_due_date_cells_are_arrays():
    """담당자와 마감일은 배열로 보낸다"""
    fields = TASK_LIST.initial_fields("작업", REQUESTER, "2026-09-02", THREAD_URL)

    assert {"column_id": "Col01", "user": [REQUESTER]} in fields
    assert {"column_id": "Col02", "date": ["2026-09-02"]} in fields


def test_empty_cells_are_omitted():
    """담당자와 마감일이 없으면 셀을 아예 보내지 않는다"""
    fields = TASK_LIST.initial_fields("작업", None, None, THREAD_URL)

    assert [field["column_id"] for field in fields] == ["Col012A3BCDE4"]


def test_completion_cells_carry_row_ids():
    """완료 셀은 행마다 row_id 를 달고 배열 값을 쓴다"""
    cells = TASK_LIST.completion_cells(["Rec01", "Rec02"])

    assert cells == [
        {"row_id": "Rec01", "column_id": "Col00", "checkbox": [True]},
        {"row_id": "Rec02", "column_id": "Col00", "checkbox": [True]},
    ]


# --- 셀 읽기 ---


def test_unchecked_item_is_not_completed():
    """완료 칸은 배열로 온다. 체크를 푼 [False] 를 완료로 읽으면 안 된다"""
    assert TASK_LIST.is_completed(item("R", checkbox=[True])) is True
    assert TASK_LIST.is_completed(item("R", checkbox=[False])) is False
    assert TASK_LIST.is_completed(item("R", checkbox=[])) is False
    assert TASK_LIST.is_completed(item("R")) is False


def test_title_of_missing_cell_is_empty():
    """제목 셀이 비어 fields 에서 빠져도 빈 문자열로 읽는다"""
    assert TASK_LIST.title_of(item("R", title="계정 생성 ↗ 슬랙")) == "계정 생성 ↗ 슬랙"
    assert TASK_LIST.title_of(item("R")) == ""


# --- 페이징 ---


async def test_list_all_items_follows_cursor():
    """완료 항목까지 함께 오므로 커서를 끝까지 따라간다"""
    client = AsyncMock()
    client.slackLists_items_list.side_effect = [
        {"items": [item("Rec01")], "response_metadata": {"next_cursor": "c1"}},
        {"items": [item("Rec02")], "response_metadata": {"next_cursor": ""}},
    ]

    items = await list_all_items(client, TASK_LIST.list_id)

    assert [row["id"] for row in items] == ["Rec01", "Rec02"]
    assert client.slackLists_items_list.await_count == 2


async def test_list_all_items_stops_at_page_cap():
    """커서가 끝나지 않아도 상한에서 멈춘다"""
    client = AsyncMock()
    client.slackLists_items_list.return_value = {
        "items": [item("Rec01")],
        "response_metadata": {"next_cursor": "endless"},
    }

    await list_all_items(client, TASK_LIST.list_id)

    assert client.slackLists_items_list.await_count == 20


# --- 도구 동작 ---


def write_tools(client) -> dict:
    """등록된 채널의 도구를 이름으로 찾을 수 있게 만든다"""
    tools = get_task_list_write_tools(client, CHANNEL, TASK_LIST, REQUESTER, THREAD_URL)
    return {tool.name: tool for tool in tools}


async def test_add_tasks_builds_cells_before_calling_slack():
    """셀을 먼저 다 만들므로 형식 오류면 API 를 한 번도 부르지 않는다"""
    client = AsyncMock()
    tools = write_tools(client)

    result = await tools["add_channel_tasks"].ainvoke(
        {
            "tasks": [
                {"title": "계정 생성"},
                {"title": "자료 정리", "due_date": "2026-09-02"},
            ]
        }
    )

    assert client.slackLists_items_create.await_count == 2
    assert "2개" in result


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


def test_task_input_requires_title():
    """도구 인자에 스키마가 붙어 title 누락이 API 호출 전에 드러난다"""
    assert TaskInput(title="작업").assignee is None
    assert "title" in TaskInput.model_json_schema()["required"]


# --- 도구 선택 분기 ---


NOTION_FACTORIES = (
    "get_create_notion_task_tool",
    "get_create_notion_follow_up_task_tool",
    "get_update_notion_task_deadline_tool",
    "get_update_notion_task_status_tool",
    "get_notion_page_tool",
)


async def build_tools_with(channel: str, task_list: ChannelTaskList | None) -> set[str]:
    """노션 도구를 이름표로 대신 세우고 _build_tools 가 고른 도구 이름을 모은다"""
    with patch.object(general, "find_channel_task_list", return_value=task_list):
        with patch.multiple(
            general,
            **{name: lambda *a, **k: "notion_tool" for name in NOTION_FACTORIES},
        ):
            with patch.object(general, "_get_user_squad", AsyncMock(return_value=None)):
                tools = await general._build_tools(
                    AsyncMock(), REQUESTER, channel, "1700000000.000100"
                )
    return {getattr(tool, "name", tool) for tool in tools}


async def test_registered_channel_swaps_notion_task_tools_for_list_tools():
    """등록된 채널에서는 리스트 도구가 노션 작업 생성 도구를 대신한다"""
    names = await build_tools_with(CHANNEL, TASK_LIST)

    assert {
        "add_channel_tasks",
        "complete_channel_tasks",
        "disable_channel_task_list",
    } <= names
    assert "enable_channel_task_list" not in names


async def test_unregistered_channel_keeps_notion_and_offers_enable():
    """등록 안 된 채널은 노션 흐름 그대로에 등록 도구만 더한다"""
    names = await build_tools_with(CHANNEL, None)

    assert "notion_tool" in names
    assert "enable_channel_task_list" in names
    assert "add_channel_tasks" not in names


async def test_dm_never_offers_task_list():
    """DM 은 리스트를 붙일 수 없어 등록 도구도 주지 않는다"""
    names = await build_tools_with("D0XXXX", None)

    assert "notion_tool" in names
    assert "enable_channel_task_list" not in names
