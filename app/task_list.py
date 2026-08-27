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
from pydantic import BaseModel, Field, field_validator
from slack_sdk.web.async_client import AsyncWebClient

from service.slack_task_list import (
    ChannelTaskList,
    create_channel_task_list,
    delete_channel_task_list,
    list_all_items,
    remove_task_list_bookmark,
    validate_task_list_channel,
)

# 매칭에 실패했을 때 되돌려줄 미완료 작업 수. 오래된 리스트의 제목을 전부
# 돌려주면 그대로 LLM 컨텍스트가 된다.
MAX_PENDING_SHOWN = 20


class TaskInput(BaseModel):
    """리스트에 추가할 작업 하나

    담당자와 마감일에 형식을 걸어 둡니다. 에이전트가 "다음주 화요일"이나
    "@홍길동" 을 넘기면 슬랙이 거절하는데, 그때는 이미 앞선 작업이 리스트에
    들어간 뒤라 재시도가 중복을 만듭니다.
    """

    title: str = Field(
        min_length=1, description="작업 제목. 무엇을 하는지 한 문장으로."
    )
    assignee: str | None = Field(
        default=None,
        pattern=r"^[UW][A-Z0-9]+$",
        description=(
            "담당자의 슬랙 사용자 ID (U로 시작). 대화에서 특정되지 않으면 생략한다."
        ),
    )
    due_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="마감일 (YYYY-MM-DD). 대화에서 언급되지 않으면 생략한다.",
    )

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        """공백뿐인 제목은 링크만 붙은 빈 행이 되므로 막는다"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("작업 제목이 비어 있습니다.")
        return stripped


def get_enable_task_list_tools(client: AsyncWebClient, channel: str) -> list:
    """작업 리스트를 아직 쓰지 않는 채널에 주는 도구를 반환합니다.

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
        info = (await client.conversations_info(channel=channel))["channel"]
        rejection = validate_task_list_channel(info)
        if rejection:
            return rejection

        task_list = await create_channel_task_list(client, channel, info["name"])
        return (
            f"작업 리스트를 만들어 채널에 공유하고 북마크에 걸었습니다:"
            f" {task_list.list_url}\n"
            "이제 작업을 만들어 달라고 하면 여기에 쌓입니다."
            " 마감일 알림은 리스트 화면의 자동화에서 켤 수 있습니다."
        )

    return [enable_channel_task_list]


def get_task_list_write_tools(
    client: AsyncWebClient,
    channel: str,
    task_list: ChannelTaskList,
    requester_id: str | None,
    thread_url: str,
) -> list:
    """작업 리스트를 쓰는 채널에 주는 도구를 반환합니다.

    Args:
        client: 슬랙 클라이언트
        channel: 멘션이 온 채널 ID
        task_list: 이 채널의 작업 리스트
        requester_id: 요청한 사람의 슬랙 사용자 ID
        thread_url: 요청이 오간 슬랙 스레드 URL

    Returns:
        list: [작업 추가, 완료 처리, 등록 해제] 도구
    """

    @tool
    async def add_channel_tasks(tasks: list[TaskInput]) -> str:
        """
        이 채널의 작업 리스트에 작업을 추가합니다.
        슬랙 대화를 정리해 작업을 만들어 달라는 요청에 사용합니다.
        담당자를 비우면 요청한 사람이 담당자가 됩니다.

        Returns:
            추가 결과와 리스트 URL
        """
        for task in tasks:
            await client.slackLists_items_create(
                list_id=task_list.list_id,
                initial_fields=task_list.initial_fields(
                    task.title,
                    task.assignee or requester_id,
                    task.due_date,
                    thread_url,
                ),
            )

        return f"{len(tasks)}개의 작업을 추가했습니다: {task_list.list_url}"

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

        items, truncated = await list_all_items(client, task_list.list_id)
        pending = [
            (item["id"], task_list.title_of(item))
            for item in items
            if not task_list.is_completed(item)
        ]
        matched = [
            (row_id, title)
            for row_id, title in pending
            if any(wanted_title in title for wanted_title in wanted)
        ]

        if not matched:
            shown = [title for _, title in pending][:MAX_PENDING_SHOWN]
            more = len(pending) - len(shown)
            return (
                "일치하는 작업이 없습니다. 미완료 작업: "
                + ", ".join(shown)
                + (f" 외 {more}개" if more else "")
                + (f" (앞 {len(items)}개만 확인했습니다)" if truncated else "")
            )

        await client.slackLists_items_update(
            list_id=task_list.list_id,
            cells=task_list.completion_cells([row_id for row_id, _ in matched]),
        )

        return f"{len(matched)}개를 완료 처리했습니다: " + ", ".join(
            title for _, title in matched
        )

    @tool
    async def disable_channel_task_list() -> str:
        """
        이 채널의 작업을 다시 노션에 만들도록 되돌립니다.
        "이 채널 작업 리스트 그만 쓸래" 같은 요청에 사용합니다.
        """
        list_url = await asyncio.to_thread(delete_channel_task_list, channel)
        if list_url is None:
            return "이 채널은 작업 리스트를 쓰고 있지 않았습니다."

        await remove_task_list_bookmark(client, channel, list_url)
        return (
            "작업 리스트 연결을 끊고 북마크를 걷었습니다. 이제 작업은 노션에 생깁니다."
            f" 쌓인 작업은 리스트에 그대로 있습니다: {list_url}"
        )

    return [add_channel_tasks, complete_channel_tasks, disable_channel_task_list]
