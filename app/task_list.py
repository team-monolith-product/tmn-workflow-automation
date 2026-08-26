"""
채널의 작업을 슬랙 리스트로 관리하는 도구입니다.

캔버스가 아니라 리스트를 쓰는 이유가 있습니다. 캔버스 API에는 본문을
돌려주는 엔드포인트가 없어서 무엇이 적혀 있는지 봇이 읽지 못합니다.
완료 처리를 말로 받으려면 지금 목록을 읽어야 하는데 거기서 막힙니다.

채널 ID를 도구 인자가 아니라 클로저로 받습니다. 멘션이 온 그 채널이
대상이라는 걸 강제해서, 에이전트가 다른 채널을 지어내는 경로를 없앱니다.
app/knowledge.py 의 수집 등록 도구와 같은 방식입니다.
"""

import asyncio

from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.slack_task_list import (
    ChannelTaskList,
    create_channel_task_list,
    find_channel_task_list,
)

# 항목 조회 한 페이지 크기. 완료된 항목도 같이 오므로 한 페이지로는 부족하다.
ITEM_PAGE_SIZE = 100


def build_title_cell(column_id: str, title: str, thread_url: str) -> dict:
    """제목 셀을 만듭니다. 제목 뒤에 슬랙 스레드 링크를 붙입니다.

    todo_mode 리스트에는 제목 열 하나와 할 일 열 셋만 있고 만든 뒤 열을
    추가하는 API가 없습니다. 출처를 남기려면 제목 칸 안에 넣어야 합니다.

    Args:
        column_id: 제목 열의 ID
        title: 작업 제목
        thread_url: 요청이 오간 슬랙 스레드 URL

    Returns:
        dict: slackLists.items.create 의 필드 하나
    """
    return {
        "column_id": column_id,
        "rich_text": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": f"{title} "},
                            {"type": "link", "url": thread_url, "text": "↗ 슬랙"},
                        ],
                    }
                ],
            }
        ],
    }


def build_initial_fields(
    task: dict,
    task_list: ChannelTaskList,
    requester_id: str | None,
    thread_url: str,
) -> list[dict]:
    """작업 하나를 리스트 셀 목록으로 바꿉니다.

    Args:
        task: title·assignee·due_date 를 담은 작업
        task_list: 대상 리스트
        requester_id: 요청한 사람의 슬랙 사용자 ID
        thread_url: 요청이 오간 슬랙 스레드 URL

    Returns:
        list[dict]: slackLists.items.create 의 initial_fields
    """
    fields = [build_title_cell(task_list.name_column_id, task["title"], thread_url)]

    assignee = task.get("assignee") or requester_id
    if assignee:
        fields.append({"column_id": task_list.assignee_column_id, "user": [assignee]})

    due_date = task.get("due_date")
    if due_date:
        fields.append({"column_id": task_list.due_date_column_id, "date": [due_date]})

    return fields


def read_cell(item: dict, column_id: str, value_key: str):
    """항목에서 특정 열의 값을 읽습니다.

    Args:
        item: slackLists.items.list 의 항목 하나
        column_id: 읽을 열의 ID
        value_key: 값이 담긴 키. 제목은 text, 완료는 checkbox

    Returns:
        셀 값. 그 열이 없으면 None
    """
    for field in item["fields"]:
        if field["column_id"] == column_id:
            return field.get(value_key)
    return None


async def _list_all_items(client: AsyncWebClient, list_id: str) -> list[dict]:
    """리스트의 항목을 전부 읽습니다.

    완료된 항목도 함께 오므로 한 페이지만 읽으면 오래된 리스트에서
    미완료 작업이 뒤로 밀려 보이지 않습니다.

    Args:
        client: 슬랙 클라이언트
        list_id: 리스트 ID

    Returns:
        list[dict]: 항목 목록
    """
    items: list[dict] = []
    cursor = None

    while True:
        response = await client.slackLists_items_list(
            list_id=list_id, limit=ITEM_PAGE_SIZE, cursor=cursor
        )
        items.extend(response["items"])
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return items


def is_completed(item: dict, completed_column_id: str) -> bool:
    """항목이 완료됐는지 봅니다.

    완료 칸의 값은 `[true]` 처럼 배열로 옵니다. 배열을 그대로 참거짓으로
    쓰면 체크를 풀어 `[false]` 가 된 항목까지 완료로 읽습니다.

    Args:
        item: slackLists.items.list 의 항목 하나
        completed_column_id: 완료 열의 ID

    Returns:
        bool: 완료됐으면 True
    """
    values = read_cell(item, completed_column_id, "checkbox")
    return bool(values and values[0])


def get_channel_task_list_tools(client: AsyncWebClient, channel: str) -> list:
    """멘션이 온 채널을 작업 리스트로 전환하는 도구를 반환합니다.

    Args:
        client: 슬랙 클라이언트
        channel: 멘션이 온 채널 ID

    Returns:
        list: [등록 도구]
    """

    @tool
    async def enable_channel_task_list() -> str:
        """
        이 채널의 작업을 슬랙 리스트로 관리하도록 켭니다.
        "이 채널 작업은 리스트로 관리하자" 처럼 작업 관리 방식을 바꿔 달라는
        요청에만 사용합니다. 작업을 만들어 달라는 요청에는 쓰지 않습니다.
        """
        if channel.startswith("D"):
            return "채널에서만 켤 수 있습니다."

        task_list = await asyncio.to_thread(find_channel_task_list, channel)
        if task_list:
            return f"이미 이 채널의 작업 리스트가 있습니다: {task_list.url}"

        task_list = await create_channel_task_list(client, channel)
        return (
            f"작업 리스트를 만들어 채널에 공유하고 북마크에 걸었습니다:"
            f" {task_list.url}\n"
            "이제 작업을 만들어 달라고 하면 여기에 쌓입니다."
            " 마감일 알림은 리스트 화면의 자동화에서 켤 수 있습니다."
        )

    return [enable_channel_task_list]


def get_task_list_write_tools(
    client: AsyncWebClient,
    task_list: ChannelTaskList,
    requester_id: str | None,
    thread_url: str,
) -> list:
    """채널 작업 리스트를 채우고 끝내는 도구를 반환합니다.

    Args:
        client: 슬랙 클라이언트
        task_list: 이 채널의 작업 리스트
        requester_id: 요청한 사람의 슬랙 사용자 ID
        thread_url: 요청이 오간 슬랙 스레드 URL

    Returns:
        list: [작업 추가, 완료 처리] 도구
    """

    @tool
    async def add_channel_tasks(tasks: list[dict]) -> str:
        """
        이 채널의 작업 리스트에 작업을 추가합니다.
        슬랙 대화를 정리해 작업을 만들어 달라는 요청에 사용합니다.

        tasks 는 추가할 작업 목록입니다. title 이 필수이고, assignee 로
        담당자의 슬랙 사용자 ID(U로 시작), due_date 로 마감일을 YYYY-MM-DD
        꼴로 줍니다. 담당자를 비우면 요청한 사람이 담당자가 됩니다.
        예: [{"title": "계정 일괄 생성", "due_date": "2026-09-02"}]

        Returns:
            추가 결과와 리스트 URL
        """
        for task in tasks:
            await client.slackLists_items_create(
                list_id=task_list.list_id,
                initial_fields=build_initial_fields(
                    task, task_list, requester_id, thread_url
                ),
            )

        return f"{len(tasks)}개의 작업을 추가했습니다: {task_list.url}"

    @tool
    async def complete_channel_tasks(titles: list[str]) -> str:
        """
        이 채널의 작업 리스트에서 작업을 완료로 표시합니다.
        "그 작업 완료 처리해줘" 같은 요청에 사용합니다.

        titles 는 완료할 작업의 제목입니다. 리스트에 적힌 제목의 일부만
        넘겨도 되지만, 여러 작업에 걸릴 만큼 짧게 주면 그만큼 한꺼번에
        완료됩니다. 일치하는 작업이 없으면 미완료 작업 목록을 돌려주니
        그중에서 골라 다시 부르면 됩니다.

        Returns:
            완료 처리한 작업, 또는 미완료 작업 목록
        """
        wanted = [title.strip() for title in titles if title.strip()]
        if not wanted:
            return "완료할 작업의 제목을 알려주세요."

        matched: list[tuple[str, str]] = []
        pending: list[str] = []
        for item in await _list_all_items(client, task_list.list_id):
            if is_completed(item, task_list.completed_column_id):
                continue
            item_title = read_cell(item, task_list.name_column_id, "text") or ""
            if any(title in item_title for title in wanted):
                matched.append((item["id"], item_title))
            else:
                pending.append(item_title)

        if not matched:
            return "일치하는 작업이 없습니다. 미완료 작업: " + ", ".join(pending)

        for row_id, _ in matched:
            await client.slackLists_items_update(
                list_id=task_list.list_id,
                cells=[
                    {
                        "row_id": row_id,
                        "column_id": task_list.completed_column_id,
                        "checkbox": [True],
                    }
                ],
            )

        return f"{len(matched)}개를 완료 처리했습니다: " + ", ".join(
            title for _, title in matched
        )

    return [add_channel_tasks, complete_channel_tasks]
