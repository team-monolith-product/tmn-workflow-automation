"""
채널별 슬랙 작업 리스트의 Service Layer입니다.

channel_task_list 표는 기존 Slack 봇의 라우팅 설정입니다. 채널에서 새 작업
요청이 왔을 때 어느 List에 행을 만들지 정하고, 행이 없으면 Notion 흐름을
유지합니다. Slack List 행과 작업 스레드의 관계는 저장하지 않습니다.

열 ID는 새 행을 만들 때 기존 행 ID가 없어 items.info를 호출할 수 없는 봇
경로를 위해 저장합니다. 운영 MCP도 새 행의 대상 채널을 고르고 요청 맥락 없는
행의 작업 채널을 찾을 때 이 라우팅을 사용합니다.

셀을 읽고 쓰는 방법도 여기 둡니다. 열 ID를 아는 곳과 그 열에 무엇을 써넣는지
아는 곳이 갈리면 슬랙이 표기를 바꿀 때 두 계층을 같이 고쳐야 합니다.

리스트를 채널에 공유하면 슬랙이 상단 탭으로 걸어 줍니다. 사람이 리스트로 바로
가는 길은 그것으로 끝이라 따로 손댈 자리가 없습니다.
"""

import asyncio
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from service.db import connect, fetch_all, fetch_one

# 리스트를 만들 때 우리가 정의하는 열. todo_mode 가 완료·담당자·마감일 셋을
# 뒤에 알아서 붙이므로 여기에는 제목과 두 스레드 열만 둔다.
#
# 슬랙 열이 message 타입인 이유가 있다. link 로 두면 URL 문자열만 남지만
# message 는 슬랙이 채널과 ts 를 풀어 카드로 보여 주고, 값이 배열이라 작업
# 하나에 스레드를 여럿 달 수 있다.
CREATE_SCHEMA = [
    {"key": "name", "name": "작업", "type": "text", "is_primary_column": True},
    {"key": "slack_thread", "name": "요청 맥락", "type": "message"},
    {"key": "work_thread", "name": "작업 기록", "type": "message"},
]

# 항목 조회 한 페이지 크기. 완료된 항목도 같이 오므로 한 페이지로는 부족하다.
ITEM_PAGE_SIZE = 100

# 커서가 끝나지 않을 때 호출이 무한히 이어지지 않도록 둔 상한
MAX_ITEM_PAGES = 20

# 마감일이 빈 작업은 List 자동화가 집지 못하므로 모든 생성 경로에 적용한다.
DEFAULT_DUE_DAYS = 7
KST = ZoneInfo("Asia/Seoul")


def default_due_date() -> str:
    """기본 마감일을 한국 날짜 기준으로 반환합니다."""
    return (datetime.now(KST) + timedelta(days=DEFAULT_DUE_DAYS)).date().isoformat()


def build_completion_cells(column_id: str, row_ids: list[str]) -> list[dict]:
    """Slack List 행을 완료로 표시할 boolean 셀을 만듭니다."""
    return [
        {"row_id": row_id, "column_id": column_id, "checkbox": True}
        for row_id in row_ids
    ]


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
        if field.get("column_id") == column_id:
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
    thread_column_id: str | None = None

    def initial_fields(
        self,
        title: str,
        assignee: str | None,
        due_date: str | None,
        thread_url: str | None,
    ) -> list[dict]:
        """작업 하나를 slackLists.items.create 의 initial_fields 로 만듭니다.

        스레드 링크는 슬랙 열에 따로 넣습니다. 제목에 섞으면 제목으로 작업을
        찾는 완료 처리가 링크 텍스트까지 같이 읽습니다.

        thread_column_id 가 없는 리스트는 이 변경 전에 만들어진 것입니다. 열을
        나중에 추가하는 API가 없으니 그런 리스트는 예전처럼 제목 칸에 링크를
        붙입니다.

        Args:
            title: 작업 제목
            assignee: 담당자의 슬랙 사용자 ID
            due_date: 마감일 (YYYY-MM-DD)
            thread_url: 요청이 오간 슬랙 스레드 URL

        Returns:
            list[dict]: 셀 목록
        """
        if self.thread_column_id or not thread_url:
            title_elements = [{"type": "text", "text": title}]
        else:
            title_elements = [
                {"type": "text", "text": f"{title} "},
                {"type": "link", "url": thread_url, "text": "↗ 슬랙"},
            ]

        cells = [
            {
                "column_id": self.name_column_id,
                "rich_text": [
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": title_elements,
                            }
                        ],
                    }
                ],
            }
        ]

        if self.thread_column_id and thread_url:
            cells.append({"column_id": self.thread_column_id, "message": [thread_url]})

        if assignee:
            cells.append({"column_id": self.assignee_column_id, "user": [assignee]})

        if due_date:
            cells.append({"column_id": self.due_date_column_id, "date": [due_date]})

        return cells

    async def create_task(
        self,
        client: AsyncWebClient,
        title: str,
        *,
        assignee: str | None = None,
        due_date: str | None = None,
        source_thread_url: str | None = None,
    ) -> str:
        """작업 행을 만들고 행 URL을 반환합니다."""
        title = title.strip()
        if not title:
            raise ValueError("작업 제목이 비어 있습니다.")
        due_date = due_date or default_due_date()
        try:
            date.fromisoformat(due_date)
        except ValueError as exc:
            raise ValueError("마감일은 YYYY-MM-DD 형식이어야 합니다.") from exc

        created = await client.slackLists_items_create(
            list_id=self.list_id,
            initial_fields=self.initial_fields(
                title, assignee, due_date, source_thread_url
            ),
        )
        return f"{self.list_url}?record_id={created['item']['id']}"

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
        return build_completion_cells(self.completed_column_id, row_ids)


# 표의 열 이름은 dataclass 필드에서 딴다. 손으로 나열하면 필드를 추가했을 때
# psycopg 가 남는 키를 무시해 INSERT 가 조용히 그 열을 빠뜨린다.
TASK_LIST_COLUMNS = tuple(field.name for field in fields(ChannelTaskList))

SELECT_TASK_LIST = f"""
SELECT {", ".join(TASK_LIST_COLUMNS)}
FROM channel_task_list
WHERE channel_id = %(channel_id)s
"""

SELECT_TASK_LIST_CHANNELS = """
SELECT channel_id, list_id
FROM channel_task_list
ORDER BY channel_id
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

    슬랙 열은 get 으로 꺼냅니다. 이 변경 전에 만들어진 리스트에는 그 열이
    없고, 그때는 None 이 들어가 제목 칸에 링크를 붙이는 예전 경로를 탑니다.

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
        thread_column_id=columns.get("slack_thread"),
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


def list_task_list_channels() -> dict[str, str]:
    """등록된 채널 ID와 각 채널의 Slack List ID를 반환합니다."""
    with connect(read_only=True) as conn:
        rows = fetch_all(conn, SELECT_TASK_LIST_CHANNELS)
    return {row["channel_id"]: row["list_id"] for row in rows}


def find_task_list_channel_id(list_id: str) -> str | None:
    """List가 연결된 채널을 반환하고, 여러 채널이면 모호함을 드러냅니다."""
    channels = [
        channel_id
        for channel_id, registered_list_id in list_task_list_channels().items()
        if registered_list_id == list_id
    ]
    if len(channels) > 1:
        raise ValueError("하나의 Slack 작업 List가 여러 채널에 연결되어 있습니다.")
    return channels[0] if channels else None


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


async def create_channel_task_list(
    client: AsyncWebClient, channel_id: str, channel_name: str
) -> ChannelTaskList:
    """슬랙 리스트를 만들어 채널에 공유하고 등록합니다.

    schema 로 제목과 요청 맥락·작업 기록 열을 정의하고 todo_mode 가 완료·담당자·마감일을
    뒤에 붙입니다. 둘은 같이 씁니다.

    열은 만들 때만 정할 수 있고 뒤에 추가하는 API가 없습니다. 그래서 새 List는
    두 message 열을 처음부터 같이 만듭니다.

    리스트를 만든 직후 표에 넣고 공유를 뒤에 합니다. 공유가 실패해도 만들어진
    리스트를 표가 알고 있어야, 다시 켤 때 리스트가 하나씩 더 생기지 않습니다.
    만들기와 저장 사이만은 원자적이지 않아서, 그 사이에 끊기면 리스트가 표에
    없는 채로 남습니다.

    공유하면 슬랙이 그 리스트를 채널 상단 탭으로 걸어 줍니다. 탭을 다루는 API
    는 없고 필요하지도 않습니다.

    Args:
        client: 슬랙 클라이언트
        channel_id: 슬랙 채널 ID
        channel_name: 리스트 이름을 지을 채널 이름

    Returns:
        ChannelTaskList: 만들어진 작업 리스트
    """
    created = await client.slackLists_create(
        name=f"{channel_name} 작업", todo_mode=True, schema=CREATE_SCHEMA
    )
    list_id = created["list_id"]

    auth = await client.auth_test()
    list_url = f"{auth['url'].rstrip('/')}/lists/{auth['team_id']}/{list_id}"

    task_list = to_task_list(list_id, list_url, created["list_metadata"]["schema"])
    await asyncio.to_thread(save_channel_task_list, channel_id, task_list)

    await client.slackLists_access_set(
        list_id=list_id, access_level="write", channel_ids=[channel_id]
    )

    return task_list
