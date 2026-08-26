"""
채널별 슬랙 작업 리스트의 Service Layer입니다.

channel_task_list 표가 SOT입니다. 채널이 작업을 리스트로 관리하는지는 이 표에
행이 있느냐로만 정해집니다.

열 ID를 표에 두는 이유가 있습니다. 슬랙에는 리스트의 열을 조회하는 API가
없어서 slackLists.create 응답이 열 ID를 아는 유일한 자리입니다. 그때 받아
저장해 두지 않으면 다시 알아낼 방법이 없습니다.

셀을 읽고 쓰는 방법도 여기 둡니다. 열 ID를 아는 곳과 그 열에 무엇을 써넣는지
아는 곳이 갈리면 슬랙이 표기를 바꿀 때 두 계층을 같이 고쳐야 합니다.

채널 북마크도 걸지만 사람이 리스트로 바로 가는 용도일 뿐이고, 봇이 읽는
자리는 아닙니다.
"""

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from service.db import connect, fetch_one

BOOKMARK_TITLE = "작업 리스트"

# 항목 조회 한 페이지 크기. 완료된 항목도 같이 오므로 한 페이지로는 부족하다.
ITEM_PAGE_SIZE = 100

# 커서가 끝나지 않을 때 호출이 무한히 이어지지 않도록 둔 상한
MAX_ITEM_PAGES = 20

TASK_LIST_COLUMNS = (
    "list_id",
    "list_url",
    "name_column_id",
    "completed_column_id",
    "assignee_column_id",
    "due_date_column_id",
)

SELECT_TASK_LIST = f"""
SELECT {", ".join(TASK_LIST_COLUMNS)}
FROM channel_task_list
WHERE channel_id = %(channel_id)s
"""

INSERT_TASK_LIST = f"""
INSERT INTO channel_task_list (channel_id, {", ".join(TASK_LIST_COLUMNS)})
VALUES (%(channel_id)s, {", ".join(f"%({name})s" for name in TASK_LIST_COLUMNS)})
"""

DELETE_TASK_LIST = """
DELETE FROM channel_task_list
WHERE channel_id = %(channel_id)s
RETURNING list_url
"""


def _read_cell(item: dict, column_id: str, value_key: str):
    """항목에서 특정 열의 값을 읽습니다.

    값이 비어 있는 셀은 응답의 fields 에서 통째로 빠지므로 못 찾는 것이
    정상입니다. 셀이 하나도 없는 항목은 fields 자체가 없습니다.

    Args:
        item: slackLists.items.list 의 항목 하나
        column_id: 읽을 열의 ID
        value_key: 값이 담긴 키. 제목은 text, 완료는 checkbox

    Returns:
        셀 값. 그 열이 없으면 None
    """
    for field in item.get("fields", []):
        if field["column_id"] == column_id:
            return field.get(value_key)
    return None


@dataclass(frozen=True)
class ChannelTaskList:
    """채널에 연결된 슬랙 작업 리스트

    열 ID를 들고 있으면서 그 열의 셀을 읽고 쓰는 방법도 함께 압니다.
    """

    list_id: str
    list_url: str
    name_column_id: str
    completed_column_id: str
    assignee_column_id: str
    due_date_column_id: str

    def initial_fields(
        self,
        title: str,
        assignee: str | None,
        due_date: str | None,
        thread_url: str,
    ) -> list[dict]:
        """작업 하나를 slackLists.items.create 의 initial_fields 로 만듭니다.

        제목 칸에 스레드 링크를 함께 넣습니다. todo_mode 리스트에는 제목 열
        하나와 할 일 열 셋만 있고 만든 뒤 열을 추가하는 API가 없어서, 출처를
        남길 자리가 제목 칸뿐입니다.

        Args:
            title: 작업 제목
            assignee: 담당자의 슬랙 사용자 ID
            due_date: 마감일 (YYYY-MM-DD)
            thread_url: 요청이 오간 슬랙 스레드 URL

        Returns:
            list[dict]: 셀 목록
        """
        fields = [
            {
                "column_id": self.name_column_id,
                "rich_text": [
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [
                                    {"type": "text", "text": f"{title} "},
                                    {
                                        "type": "link",
                                        "url": thread_url,
                                        "text": "↗ 슬랙",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        if assignee:
            fields.append({"column_id": self.assignee_column_id, "user": [assignee]})

        if due_date:
            fields.append({"column_id": self.due_date_column_id, "date": [due_date]})

        return fields

    def title_of(self, item: dict) -> str:
        """항목의 제목을 읽습니다.

        Args:
            item: slackLists.items.list 의 항목 하나

        Returns:
            str: 제목. 없으면 빈 문자열
        """
        return _read_cell(item, self.name_column_id, "text") or ""

    def is_completed(self, item: dict) -> bool:
        """항목이 완료됐는지 봅니다.

        완료 칸의 값은 `[true]` 처럼 배열로 옵니다. 배열을 그대로 참거짓으로
        쓰면 체크를 풀어 `[false]` 가 된 항목까지 완료로 읽습니다.

        Args:
            item: slackLists.items.list 의 항목 하나

        Returns:
            bool: 완료됐으면 True
        """
        values = _read_cell(item, self.completed_column_id, "checkbox")
        return bool(values and values[0])

    def completion_cells(self, row_ids: list[str]) -> list[dict]:
        """여러 행을 한 번에 완료로 표시할 셀 목록을 만듭니다.

        Args:
            row_ids: 완료 처리할 행 ID 목록

        Returns:
            list[dict]: slackLists.items.update 의 cells
        """
        return [
            {
                "row_id": row_id,
                "column_id": self.completed_column_id,
                "checkbox": [True],
            }
            for row_id in row_ids
        ]


def validate_task_list_channel(info: dict[str, Any]) -> str | None:
    """작업 리스트를 붙일 수 있는 채널인지 검사합니다.

    외부와 공유된 채널은 막습니다. 리스트를 채널에 공유하면 그 채널 멤버가
    쓰기 권한을 얻는데, 슬랙 커넥트 채널에서는 그 멤버에 외부 조직이 들어
    있습니다. DM 은 리스트를 붙일 자리가 없습니다.

    Args:
        info: conversations.info 응답의 channel 객체

    Returns:
        str | None: 거절 사유. 붙일 수 있으면 None
    """
    if info.get("is_im") or info.get("is_mpim"):
        return "채널에서만 작업 리스트를 켤 수 있습니다."
    if info.get("is_ext_shared") or info.get("is_pending_ext_shared"):
        return (
            "외부와 공유된 채널에는 켤 수 없습니다."
            " 리스트를 공유하면 외부 조직도 작업을 고칠 수 있습니다."
        )
    return None


def to_task_list(
    list_id: str, list_url: str, schema: list[dict[str, Any]]
) -> ChannelTaskList:
    """slackLists.create 응답의 schema로 ChannelTaskList를 만듭니다.

    제목 열은 key가 리스트마다 다를 수 있어 is_primary_column으로 찾습니다.

    Args:
        list_id: 리스트 ID
        list_url: 리스트 URL
        schema: list_metadata.schema

    Returns:
        ChannelTaskList: 작업 리스트
    """
    columns = {
        ("name" if column.get("is_primary_column") else column["key"]): column["id"]
        for column in schema
    }
    return ChannelTaskList(
        list_id=list_id,
        list_url=list_url,
        name_column_id=columns["name"],
        completed_column_id=columns["todo_completed"],
        assignee_column_id=columns["todo_assignee"],
        due_date_column_id=columns["todo_due_date"],
    )


def find_channel_task_list(channel_id: str) -> ChannelTaskList | None:
    """채널에 연결된 작업 리스트를 조회합니다. psycopg는 동기입니다.

    Args:
        channel_id: 슬랙 채널 ID

    Returns:
        ChannelTaskList | None: 등록되지 않은 채널이면 None
    """
    with connect(read_only=True) as conn:
        row = fetch_one(conn, SELECT_TASK_LIST, {"channel_id": channel_id})
    return ChannelTaskList(**row) if row else None


def save_channel_task_list(channel_id: str, task_list: ChannelTaskList) -> None:
    """채널과 작업 리스트의 연결을 저장합니다. psycopg는 동기입니다.

    UPSERT 가 아닙니다. 등록은 이미 없을 때만 부르므로, 경합으로 두 번 들어오면
    유니크 위반으로 드러나는 편이 이전 리스트 링크가 말없이 사라지는 것보다
    낫습니다.

    Args:
        channel_id: 슬랙 채널 ID
        task_list: 저장할 작업 리스트
    """
    with connect() as conn:
        conn.execute(INSERT_TASK_LIST, {"channel_id": channel_id, **asdict(task_list)})


def delete_channel_task_list(channel_id: str) -> str | None:
    """채널과 작업 리스트의 연결을 끊습니다. psycopg는 동기입니다.

    슬랙 리스트 자체는 그대로 둡니다. 쌓인 작업을 지우지 않고 작업이 가는
    곳만 노션으로 되돌립니다.

    Args:
        channel_id: 슬랙 채널 ID

    Returns:
        str | None: 끊긴 리스트 URL. 등록된 적 없으면 None
    """
    with connect() as conn:
        row = fetch_one(conn, DELETE_TASK_LIST, {"channel_id": channel_id})
    return row["list_url"] if row else None


async def list_all_items(
    client: AsyncWebClient, list_id: str
) -> tuple[list[dict], bool]:
    """리스트의 항목을 읽습니다.

    완료된 항목도 함께 오므로 한 페이지만 읽으면 오래된 리스트에서
    미완료 작업이 뒤로 밀려 보이지 않습니다.

    Args:
        client: 슬랙 클라이언트
        list_id: 리스트 ID

    Returns:
        tuple[list[dict], bool]: 항목 목록과, 상한에 걸려 잘렸는지 여부
    """
    items: list[dict] = []
    cursor = None

    for _ in range(MAX_ITEM_PAGES):
        response = await client.slackLists_items_list(
            list_id=list_id, limit=ITEM_PAGE_SIZE, cursor=cursor
        )
        items.extend(response["items"])
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return items, False

    return items, True


async def remove_task_list_bookmark(
    client: AsyncWebClient, channel_id: str, list_url: str
) -> None:
    """채널 북마크에서 그 리스트를 가리키는 것을 걷어냅니다.

    남겨 두면 다시 켤 때 같은 이름의 북마크가 하나 더 붙어, 서로 다른 리스트를
    가리키는 북마크 둘이 채널에 남습니다.

    Args:
        client: 슬랙 클라이언트
        channel_id: 슬랙 채널 ID
        list_url: 걷어낼 리스트의 URL
    """
    response = await client.bookmarks_list(channel_id=channel_id)
    for bookmark in response["bookmarks"]:
        if bookmark.get("link") == list_url:
            await client.bookmarks_remove(
                channel_id=channel_id, bookmark_id=bookmark["id"]
            )


async def create_channel_task_list(
    client: AsyncWebClient, channel_id: str, channel_name: str
) -> ChannelTaskList:
    """슬랙 리스트를 만들어 채널에 공유하고 등록합니다.

    todo_mode로 만들면 완료·담당자·마감일 열이 함께 생겨 우리가 스키마를
    짤 일이 없습니다.

    리스트를 만든 직후 표에 넣고 공유와 북마크를 뒤에 합니다. 뒤쪽이 실패해도
    만들어진 리스트를 표가 알고 있어야, 다시 켤 때 리스트가 하나씩 더 생기지
    않습니다. 만들기와 저장 사이만은 원자적이지 않아서, 그 사이에 끊기면
    리스트가 표에 없는 채로 남습니다.

    Args:
        client: 슬랙 클라이언트
        channel_id: 슬랙 채널 ID
        channel_name: 리스트 이름을 지을 채널 이름

    Returns:
        ChannelTaskList: 만들어진 작업 리스트
    """
    created = await client.slackLists_create(
        name=f"{channel_name} 작업", todo_mode=True
    )
    list_id = created["list_id"]

    auth = await client.auth_test()
    list_url = f"{auth['url'].rstrip('/')}/lists/{auth['team_id']}/{list_id}"

    task_list = to_task_list(list_id, list_url, created["list_metadata"]["schema"])
    await asyncio.to_thread(save_channel_task_list, channel_id, task_list)

    await client.slackLists_access_set(
        list_id=list_id, access_level="write", channel_ids=[channel_id]
    )

    await client.bookmarks_add(
        channel_id=channel_id,
        title=BOOKMARK_TITLE,
        type="link",
        link=list_url,
        emoji=":white_check_mark:",
    )

    return task_list
