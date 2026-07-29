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
import re
from datetime import datetime, timedelta
from typing import Literal

from langchain_core.tools import tool
from md2notionpage.core import parse_md
from pydantic import BaseModel, Field

from ..common import (
    KST,
    extract_matching_lines,
    flatten_deep_children,
    get_data_source_id,
    get_data_source_schema,
    notion,
    notion_page_to_markdown,
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


# 속성 필터로 좁힌 뒤 남길 후보 수
MAX_CANDIDATES = 30

# 본문까지 읽을 페이지 수. 노션은 평균 초당 3요청이고 페이지 하나를 마크다운으로
# 바꾸는 데도 여러 요청이 나가므로, 이 값을 올리면 체감 지연이 바로 늘어난다.
MAX_BODY_READS = 5


def _page_title(page: dict) -> str:
    for value in page["properties"].values():
        if value["type"] == "title" and value["title"]:
            return value["title"][0]["plain_text"]
    return "(제목 없음)"


def _page_summary(page: dict) -> str:
    properties = page["properties"]
    status = (properties.get("상태", {}).get("status") or {}).get("name", "-")
    task_type = (properties.get("유형", {}).get("select") or {}).get("name", "-")
    date = (properties.get("날짜", {}).get("date") or {}).get("start", "-")
    return f"- {_page_title(page)} | {task_type} | {status} | {date} | {page['url']}"


def _search_ops_tasks(
    data_source_id: str,
    keyword: str | None,
    task_type: str | None,
    status: str | None,
    since_days: int | None,
) -> str:
    """속성으로 후보를 좁힌 뒤, 필요하면 소수의 본문만 읽어 일치 부분을 추린다."""
    conditions: list[dict] = []
    if task_type:
        conditions.append({"property": "유형", "select": {"equals": task_type}})
    if status:
        conditions.append({"property": "상태", "status": {"equals": status}})
    if since_days:
        since = (datetime.now(tz=KST) - timedelta(days=since_days)).date().isoformat()
        conditions.append({"property": "날짜", "date": {"on_or_after": since}})

    query: dict = {
        "data_source_id": data_source_id,
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        "page_size": MAX_CANDIDATES,
    }
    if conditions:
        query["filter"] = conditions[0] if len(conditions) == 1 else {"and": conditions}

    pages = notion.data_sources.query(**query)["results"]

    if not pages:
        return "조건에 맞는 업무가 없습니다. 유형·상태·기간을 넓혀 다시 시도해 보세요."

    if not keyword:
        return f"업무 {len(pages)}건:\n" + "\n".join(_page_summary(p) for p in pages)

    regex = re.compile(re.escape(keyword), re.IGNORECASE)

    # 제목에서 걸린 것을 먼저 세우고, 나머지는 본문을 읽어 확인한다
    title_hits = [p for p in pages if regex.search(_page_title(p))]
    hit_ids = {p["id"] for p in title_hits}
    rest = [p for p in pages if p["id"] not in hit_ids]

    lines = [_page_summary(p) for p in title_hits]
    body_checked = 0

    for page in rest:
        if body_checked >= MAX_BODY_READS:
            break
        body_checked += 1

        body = notion_page_to_markdown(page["id"])
        matched = extract_matching_lines(body, regex)
        if matched:
            lines.append(_page_summary(page))
            lines.extend(f"    {line}" for line in matched)

    if not lines:
        return (
            f"'{keyword}'와 일치하는 업무를 찾지 못했습니다. "
            f"본문은 최근 {body_checked}건까지만 확인했으니, "
            "유형·상태·기간으로 범위를 좁히면 더 깊이 볼 수 있습니다."
        )

    remaining = len(rest) - body_checked
    footer = (
        f"\n[본문은 {body_checked}건만 확인했습니다. 후보 {remaining}건이 남아 있으니 "
        "조건을 좁히면 더 볼 수 있습니다.]"
        if remaining > 0
        else ""
    )
    return "\n".join(lines) + footer


def get_search_ops_tasks_tool(database_id: str):
    """
    운영 DB에서 과거 업무를 찾는 도구를 반환한다.

    노션은 본문 검색 API가 없어 페이지를 하나씩 읽어야 하고 평균 초당 3요청 제한이
    걸린다. 그래서 속성으로 후보를 좁힌 뒤 소수의 본문만 읽는 2단계로 동작한다.
    """
    data_source_id = get_data_source_id(database_id)
    properties_schema = get_data_source_schema(notion, data_source_id)["properties"]

    type_options = _option_names(properties_schema["유형"], "select")
    status_options = [
        option["name"] for option in properties_schema["상태"]["status"]["options"]
    ]

    TaskType = Literal[tuple(type_options)]  # type: ignore[valid-type]
    Status = Literal[tuple(status_options)]  # type: ignore[valid-type]

    class SearchOpsTasksInput(BaseModel):
        keyword: str | None = Field(
            default=None,
            description=(
                "찾을 낱말. 제목에서 먼저 찾고, 못 찾으면 최근 업무의 본문까지 읽어 "
                "확인합니다. 생략하면 조건에 맞는 업무 목록만 돌려줍니다."
            ),
        )
        task_type: TaskType | None = Field(default=None, description="업무 유형")
        status: Status | None = Field(default=None, description="업무 상태")
        since_days: int | None = Field(
            default=None, description="며칠 전까지의 업무를 볼지"
        )

    @tool("search_ops_tasks", args_schema=SearchOpsTasksInput)
    async def search_ops_tasks(
        keyword: str | None = None,
        task_type: str | None = None,
        status: str | None = None,
        since_days: int | None = None,
    ) -> str:
        """
        노션 운영 DB에서 과거에 했던 업무를 찾습니다.

        예전에 처리한 일과 관련된 질문이면 문서나 대화보다 여기를 먼저 보세요.

        유형·상태·기간으로 좁힐수록 정확하고 빠릅니다. 노션은 본문 검색을 지원하지
        않아 본문은 소수만 읽을 수 있으므로, keyword만으로 넓게 찾으면 놓칠 수
        있습니다. 조건을 함께 주세요.

        Returns:
            str: 업무 목록과 본문에서 일치한 부분
        """
        return await asyncio.to_thread(
            _search_ops_tasks,
            data_source_id,
            keyword,
            task_type,
            status,
            since_days,
        )

    return search_ops_tasks
