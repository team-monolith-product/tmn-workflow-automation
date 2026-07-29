"""
노션 운영 DB 관련 LangChain Tools

운영 DB는 기존 작업 DB와 스키마가 다르다.
- 제목 속성이 '제목'이 아니라 '이름'
- 담당자가 사람(people)이 아니라 조직 태그(multi_select)
- 구성요소/프로젝트 relation이 없고, 타임라인 대신 단일 '날짜'를 쓴다

담당자의 의미부터 다르므로 common.get_create_notion_task_tool을 재사용하지 않고
전용 도구를 둔다.
"""

import asyncio
from typing import Literal

from langchain_core.tools import tool
from md2notionpage.core import parse_md
from pydantic import BaseModel, Field

from ..common import (
    flatten_deep_children,
    get_data_source_id,
    get_data_source_schema,
    notion,
)


def _default_status_name(status_property: dict) -> str:
    """
    상태 속성에서 'To-do' 그룹의 첫 옵션을 기본 상태로 고른다.

    옵션 이름을 하드코딩하면 노션에서 이름을 바꾸는 순간 깨진다.
    그룹 이름은 노션이 고정으로 제공하므로 이쪽을 기준으로 찾는다.
    """
    status = status_property["status"]
    name_by_id = {option["id"]: option["name"] for option in status["options"]}

    for group in status.get("groups", []):
        if group["name"] == "To-do" and group["option_ids"]:
            return name_by_id[group["option_ids"][0]]

    return status["options"][0]["name"]


def _option_names(property_schema: dict, property_type: str) -> list[str]:
    """select / multi_select 속성의 옵션 이름 목록을 반환한다."""
    return [option["name"] for option in property_schema[property_type]["options"]]


def _create_ops_task(
    data_source_id: str,
    default_status: str,
    slack_thread_url: str,
    name: str,
    task_type: str,
    assignees: list[str],
    date: str | None,
    body: str | None,
) -> str:
    """운영 DB에 페이지를 생성하고 URL을 반환한다."""
    properties: dict = {
        "이름": {"title": [{"text": {"content": name}}]},
        "상태": {"status": {"name": default_status}},
        "유형": {"select": {"name": task_type}},
        "담당자": {"multi_select": [{"name": assignee} for assignee in assignees]},
    }

    if date:
        properties["날짜"] = {"date": {"start": date}}

    response = notion.pages.create(
        parent={"data_source_id": data_source_id}, properties=properties
    )
    page_id = response["id"]

    if slack_thread_url:
        notion.blocks.children.append(
            block_id=page_id,
            children=[{"type": "bookmark", "bookmark": {"url": slack_thread_url}}],
        )

    if body:
        for block in parse_md(body):
            notion.blocks.children.append(
                page_id, children=[flatten_deep_children(block)]
            )

    return response["url"]


def get_create_ops_task_tool(database_id: str, slack_thread_url: str):
    """
    운영 DB에 업무를 생성하는 도구를 반환한다.

    유형과 담당자 옵션은 노션 스키마에서 읽어 enum으로 고정하므로,
    존재하지 않는 값을 넣어 실패하는 일이 없다.

    Args:
        database_id: 운영 DB의 database ID
        slack_thread_url: 생성된 페이지에 북마크로 첨부할 슬랙 스레드 URL
    """
    data_source_id = get_data_source_id(database_id)
    properties_schema = get_data_source_schema(notion, data_source_id)["properties"]

    type_options = _option_names(properties_schema["유형"], "select")
    assignee_options = _option_names(properties_schema["담당자"], "multi_select")
    default_status = _default_status_name(properties_schema["상태"])

    TaskType = Literal[tuple(type_options)]  # type: ignore[valid-type]
    Assignee = Literal[tuple(assignee_options)]  # type: ignore[valid-type]

    class CreateOpsTaskInput(BaseModel):
        name: str = Field(description="업무 이름")
        task_type: TaskType = Field(description="업무 유형")
        assignees: list[Assignee] = Field(
            description=(
                "담당 조직. 대화에서 명확히 드러나지 않으면 추측하지 말고 "
                "사용자에게 어느 조직이 맡는지 물어보세요."
            )
        )
        date: str | None = Field(
            default=None,
            description="'YYYY-MM-DD' 형식의 일자. 정해지지 않았으면 생략.",
        )
        body: str | None = Field(
            default=None,
            description=(
                "업무 본문을 구성할 마크다운 문자열.\n"
                "**마크다운 작성 제약 사항 (반드시 준수):**\n"
                "- 서로 다른 타입의 리스트 중첩 절대 금지\n"
                "- 잘못된 예: '1) 번호 항목\\n   - 불릿 하위 항목' (번호+불릿 혼합)\n"
                "- 올바른 예: '- 불릿\\n  - 불릿 하위' (같은 타입 중첩 OK)\n"
            ),
        )

    @tool("create_ops_task", args_schema=CreateOpsTaskInput)
    async def create_ops_task(
        name: str,
        task_type: str,
        assignees: list[str],
        date: str | None = None,
        body: str | None = None,
    ) -> str:
        """
        노션 운영 DB에 업무를 생성합니다.

        발송, 시스템, 문서, 보고, 예약·구매, 섭외, 모집, CS 같은 운영 업무를 등록할 때 씁니다.
        생성된 페이지에는 이 슬랙 스레드 링크가 자동으로 첨부됩니다.

        담당자는 사람이 아니라 조직 단위입니다. 대화에서 어느 조직이 맡을지
        분명하지 않으면 이 도구를 호출하지 말고 먼저 사용자에게 물어보세요.

        Returns:
            생성된 노션 페이지의 URL
        """
        return await asyncio.to_thread(
            _create_ops_task,
            data_source_id,
            default_status,
            slack_thread_url,
            name,
            task_type,
            assignees,
            date,
            body,
        )

    return create_ops_task
