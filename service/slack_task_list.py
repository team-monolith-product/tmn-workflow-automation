"""
채널별 슬랙 작업 리스트의 Service Layer입니다.

channel_task_list 테이블이 SOT입니다. 채널이 작업을 리스트로 관리하는지는
이 표에 행이 있느냐로만 정해집니다.

열 ID를 표에 두는 이유가 있습니다. 슬랙에는 리스트의 열을 조회하는 API가
없어서 slackLists.create 응답이 열 ID를 아는 유일한 자리입니다. 그때 받아
저장해 두지 않으면 다시 알아낼 방법이 없습니다.

채널 북마크도 걸지만 사람이 리스트로 바로 가는 용도일 뿐이고, 봇이 읽는
자리는 아닙니다.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from service.knowledge.db import connect, fetch_one

BOOKMARK_TITLE = "작업 리스트"

SELECT_TASK_LIST = """
SELECT list_id, list_url, columns
FROM channel_task_list
WHERE channel_id = %(channel_id)s
"""

UPSERT_TASK_LIST = """
INSERT INTO channel_task_list (channel_id, list_id, list_url, columns)
VALUES (%(channel_id)s, %(list_id)s, %(list_url)s, %(columns)s)
ON CONFLICT (channel_id) DO UPDATE SET
    list_id  = EXCLUDED.list_id,
    list_url = EXCLUDED.list_url,
    columns  = EXCLUDED.columns
"""


@dataclass(frozen=True)
class ChannelTaskList:
    """채널에 연결된 슬랙 작업 리스트"""

    list_id: str
    url: str
    name_column_id: str
    completed_column_id: str
    assignee_column_id: str
    due_date_column_id: str


def build_list_name(channel_name: str) -> str:
    """채널 이름으로 리스트 이름을 만듭니다.

    채널 이름 전체를 쓰면 t_고객_사업운영_OO교육청 처럼 접두사까지 붙어
    리스트 목록에서 구분이 안 됩니다. 마지막 조각만 씁니다.

    Args:
        channel_name: 채널 이름

    Returns:
        str: 리스트 이름
    """
    return f"{channel_name.rsplit('_', 1)[-1]} 작업"


def columns_from_schema(schema: list[dict[str, Any]]) -> dict[str, str]:
    """slackLists.create 응답의 schema에서 열 키와 열 ID 매핑을 뽑습니다.

    제목 열은 key가 리스트마다 다를 수 있어 is_primary_column으로 찾고
    name으로 통일합니다.

    Args:
        schema: list_metadata.schema

    Returns:
        dict[str, str]: 열 키 → 열 ID
    """
    return {
        ("name" if column.get("is_primary_column") else column["key"]): column["id"]
        for column in schema
    }


def to_task_list(
    list_id: str, list_url: str, columns: dict[str, str]
) -> ChannelTaskList:
    """열 매핑을 ChannelTaskList로 바꿉니다.

    Args:
        list_id: 리스트 ID
        list_url: 리스트 URL
        columns: 열 키 → 열 ID

    Returns:
        ChannelTaskList: 작업 리스트
    """
    return ChannelTaskList(
        list_id=list_id,
        url=list_url,
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
    if row is None:
        return None
    return to_task_list(row["list_id"], row["list_url"], row["columns"])


def save_channel_task_list(
    channel_id: str, list_id: str, list_url: str, columns: dict[str, str]
) -> None:
    """채널과 작업 리스트의 연결을 저장합니다. psycopg는 동기입니다.

    Args:
        channel_id: 슬랙 채널 ID
        list_id: 리스트 ID
        list_url: 리스트 URL
        columns: 열 키 → 열 ID
    """
    with connect() as conn:
        conn.execute(
            UPSERT_TASK_LIST,
            {
                "channel_id": channel_id,
                "list_id": list_id,
                "list_url": list_url,
                "columns": json.dumps(columns, ensure_ascii=False),
            },
        )


async def create_channel_task_list(
    client: AsyncWebClient, channel_id: str
) -> ChannelTaskList:
    """슬랙 리스트를 만들어 채널에 공유하고 등록합니다.

    todo_mode로 만들면 완료·담당자·마감일 열이 함께 생겨 우리가 스키마를
    짤 일이 없습니다.

    Args:
        client: 슬랙 클라이언트
        channel_id: 슬랙 채널 ID

    Returns:
        ChannelTaskList: 만들어진 작업 리스트
    """
    info = (await client.conversations_info(channel=channel_id))["channel"]

    created = await client.slackLists_create(
        name=build_list_name(info["name"]), todo_mode=True
    )
    list_id = created["list_id"]
    columns = columns_from_schema(created["list_metadata"]["schema"])

    await client.slackLists_access_set(
        list_id=list_id, access_level="write", channel_ids=[channel_id]
    )

    auth = await client.auth_test()
    list_url = f"{auth['url'].rstrip('/')}/lists/{auth['team_id']}/{list_id}"

    # 북마크보다 먼저 저장한다. 북마크가 실패했을 때 이미 만들어 공유까지 끝난
    # 리스트를 표가 모르면, 다시 켤 때마다 리스트가 하나씩 더 생긴다.
    await asyncio.to_thread(
        save_channel_task_list, channel_id, list_id, list_url, columns
    )

    await client.bookmarks_add(
        channel_id=channel_id,
        title=BOOKMARK_TITLE,
        type="link",
        link=list_url,
        emoji=":white_check_mark:",
    )

    return to_task_list(list_id, list_url, columns)
