"""
공통 유틸리티 함수들
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Literal
from pydantic import BaseModel, Field, create_model

from cachetools import TTLCache
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from notion_to_md import NotionToMarkdown
from langchain_core.tools import tool
from langchain_community.document_loaders import WebBaseLoader
from langchain_community.tools import TavilySearchResults
from slack_sdk.web.async_client import AsyncWebClient
from md2notionpage.core import parse_md

from .tool_status_handler import ToolStatusHandler

# 환경 변수 로드
load_dotenv()

# Notion API 블록 중첩 제한 (2단계까지만 허용)
MAX_NESTING_DEPTH = 2


def flatten_deep_children(block: dict, current_depth: int = 0) -> dict:
    """
    Notion API의 블록 중첩 제한(2단계)을 초과하는 children을 평탄화한다.
    3단계 이상 중첩된 항목은 상위 항목의 텍스트에 들여쓰기로 병합된다.

    Args:
        block: Notion 블록 딕셔너리
        current_depth: 현재 중첩 깊이 (0부터 시작)

    Returns:
        중첩이 평탄화된 블록
    """
    block = block.copy()
    block_type = block.get("type")

    if not block_type:
        return block

    block_content = block.get(block_type, {})
    if not isinstance(block_content, dict):
        return block

    children = block_content.get("children", [])
    if not children:
        return block

    block_content = block_content.copy()
    block[block_type] = block_content

    if current_depth >= MAX_NESTING_DEPTH:
        # 최대 깊이에 도달: children을 텍스트로 병합
        flattened_text = _collect_nested_text(children, indent_level=1)
        if flattened_text:
            rich_text = block_content.get("rich_text", [])
            if rich_text:
                rich_text = list(rich_text) + [
                    {"type": "text", "text": {"content": flattened_text}}
                ]
            else:
                rich_text = [{"type": "text", "text": {"content": flattened_text}}]
            block_content["rich_text"] = rich_text
        del block_content["children"]
    else:
        # 깊이 여유가 있으면 재귀적으로 처리
        new_children = [
            flatten_deep_children(child, current_depth + 1) for child in children
        ]
        block_content["children"] = new_children

    return block


def _collect_nested_text(blocks: list, indent_level: int = 0) -> str:
    """
    중첩된 블록들에서 텍스트를 추출하여 들여쓰기가 적용된 문자열로 반환한다.
    """
    result = []
    indent = "  " * indent_level

    for block in blocks:
        block_type = block.get("type", "")
        block_content = block.get(block_type, {})

        if isinstance(block_content, dict):
            rich_text = block_content.get("rich_text", [])
            text = "".join(
                rt.get("text", {}).get("content", "") or rt.get("plain_text", "")
                for rt in rich_text
            )
            if text:
                result.append(f"\n{indent}• {text}")

            # 하위 children도 처리
            nested_children = block_content.get("children", [])
            if nested_children:
                nested_text = _collect_nested_text(nested_children, indent_level + 1)
                if nested_text:
                    result.append(nested_text)

    return "".join(result)


# 시간대 설정
KST = ZoneInfo("Asia/Seoul")

# 노션 클라이언트 초기화
notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))


def notion_page_to_markdown(page_id: str) -> str:
    """노션 페이지를 마크다운 문자열로 변환한다."""
    n2m = NotionToMarkdown(notion_client=notion)
    md_blocks = n2m.page_to_markdown(page_id)
    md_string_dict = n2m.to_markdown_string(md_blocks)
    return md_string_dict.get("parent", "")


_cache_slack_users = TTLCache(maxsize=100, ttl=3600)
_cache_notion_users = TTLCache(maxsize=100, ttl=3600)
_cache_database_schema = TTLCache(maxsize=10, ttl=3600)


async def slack_users_list(client: AsyncWebClient):
    """
    슬랙 사용자 목록을 조회한다.
    """
    if "slack_users_list" in _cache_slack_users:
        return _cache_slack_users["slack_users_list"]

    resp = await client.users_list()
    _cache_slack_users["slack_users_list"] = resp
    return resp


async def notion_users_list(client: NotionClient):
    """
    노션 사용자 목록을 조회한다.
    """
    if "notion_users_list" in _cache_notion_users:
        return _cache_notion_users["notion_users_list"]

    resp = client.users.list()
    _cache_notion_users["notion_users_list"] = resp
    return resp


def get_data_source_schema(client: NotionClient, data_source_id: str):
    """
    노션 데이터 소스 스키마를 조회한다.
    Notion API 2025-09-03 버전부터 database와 data source가 분리되어
    properties는 data source 레벨에 있다.
    """
    cache_key = f"data_source_schema_{data_source_id}"
    if cache_key in _cache_database_schema:
        return _cache_database_schema[cache_key]

    # data source 조회
    data_source = client.data_sources.retrieve(data_source_id)

    # properties 확인
    if "properties" not in data_source:
        raise KeyError(
            f"'properties' not found in data source {data_source_id}. "
            f"Available keys: {list(data_source.keys())}"
        )

    _cache_database_schema[cache_key] = data_source
    return data_source


def get_status_property_info(
    client: NotionClient, data_source_id: str
) -> tuple[str | None, str | None]:
    """
    노션 데이터 소스에서 status 타입 속성의 이름과 기본 상태(To-do 그룹의 첫 번째 옵션)를 반환한다.
    DB마다 상태 속성명과 옵션이 다를 수 있으므로 스키마에서 동적으로 조회한다.

    Returns:
        (속성 이름, 기본 상태 이름) 튜플. 속성이 없으면 (None, None).
    """
    ds_schema = get_data_source_schema(client, data_source_id)
    for prop_name, prop_value in ds_schema["properties"].items():
        if prop_value.get("type") == "status":
            # To-do 그룹의 첫 번째 옵션을 기본 상태로 사용
            default_status = None
            status_config = prop_value.get("status", {})
            groups = status_config.get("groups", [])
            options = status_config.get("options", [])
            option_map = {opt["id"]: opt["name"] for opt in options}

            for group in groups:
                if group.get("id") == "todo-status-group":
                    todo_option_ids = group.get("option_ids", [])
                    if todo_option_ids:
                        default_status = option_map.get(todo_option_ids[0])
                    break

            return prop_name, default_status
    return None, None


def get_status_options(client: NotionClient, data_source_id: str) -> list[str]:
    """
    노션 데이터 소스에서 상태 속성의 가능한 옵션들을 조회한다.
    """
    status_prop_name, _ = get_status_property_info(client, data_source_id)
    if not status_prop_name:
        return []

    ds_schema = get_data_source_schema(client, data_source_id)
    status_property = ds_schema["properties"][status_prop_name]

    if "status" in status_property:
        options = status_property["status"].get("options", [])
        return [option["name"] for option in options]

    return []


def get_task_type_options(client: NotionClient, data_source_id: str) -> list[str]:
    """
    노션 데이터 소스에서 유형 속성의 가능한 옵션들을 조회한다.
    """
    ds_schema = get_data_source_schema(client, data_source_id)
    task_type_property = ds_schema["properties"].get("유형", {})

    if "select" in task_type_property:
        options = task_type_property["select"].get("options", [])
        return [option["name"] for option in options]

    return []


def get_component_options(client: NotionClient, data_source_id: str) -> list[str]:
    """
    노션 데이터 소스에서 구성요소 속성의 가능한 옵션들을 조회한다.
    """
    ds_schema = get_data_source_schema(client, data_source_id)
    component_property = ds_schema["properties"].get("구성요소", {})

    if "multi_select" in component_property:
        options = component_property["multi_select"].get("options", [])
        return [option["name"] for option in options]

    return []


def get_active_projects(
    client: NotionClient, project_data_source_id: str
) -> dict[str, str]:
    """
    프로젝트 데이터 소스에서 '진행 중' 상태인 프로젝트들을 조회하여 프로젝트명:페이지ID 매핑을 반환합니다.
    """
    response = client.data_sources.query(
        data_source_id=project_data_source_id,
        filter={"property": "상태", "status": {"equals": "진행 중"}},
    )

    project_mapping = {}
    for page in response["results"]:
        # 페이지 제목 가져오기
        title_property = None
        for prop_value in page["properties"].values():
            if prop_value["type"] == "title":
                title_property = prop_value
                break

        if title_property and title_property["title"]:
            project_name = title_property["title"][0]["plain_text"]
            project_mapping[project_name] = page["id"]

    return project_mapping


search_tool = TavilySearchResults(
    max_results=10,
    search_depth="advanced",
    include_answer=True,
    include_raw_content=True,
    include_images=False,
)


@tool
def get_web_page_from_url(
    url: Annotated[str, "웹 페이지 URL"],
):
    """
    주어진 URL에서 웹 페이지를 로드하여 문서로 반환합니다.
    www.notion.so에 대한 링크는 이 도구를 사용하지 않고 get_notion_page 도구를 사용합니다.
    """
    loader = WebBaseLoader(url)
    documents = loader.load()
    return documents


async def _get_notion_assignee_id(user_email: str | None) -> str | None:
    """노션 사용자 ID를 가져옵니다."""
    if not user_email:
        return None

    notion_users = await notion_users_list(notion)
    matched_notion_user = next(
        (
            user
            for user in notion_users["results"]
            if user["type"] == "person" and user["person"]["email"] == user_email
        ),
        None,
    )
    return matched_notion_user["id"] if matched_notion_user else None


def get_create_notion_task_tool(
    user_id: str | None,
    slack_thread_url: str,
    data_source_id: str,
    client,
    project_data_source_id: str | None = None,
    title_property_name: str = "제목",
):
    """노션 작업 생성 도구를 반환합니다.

    Args:
        user_id: 슬랙 사용자 ID
        slack_thread_url: 슬랙 스레드 URL
        data_source_id: 대상 노션 데이터 소스 ID
        client: 슬랙 클라이언트
        project_data_source_id: 프로젝트 DB 데이터 소스 ID (없으면 프로젝트 필드 생략)
        title_property_name: 대상 DB의 제목 프로퍼티 이름
    """

    status_property_name, default_status = get_status_property_info(
        notion, data_source_id
    )

    async def get_assignee_id():
        if user_id is None:
            return None
        # 사용자 정보 조회
        user_info_list = await slack_users_list(client)
        user_dict = {user["id"]: user for user in user_info_list["members"]}
        user_profile = user_dict.get(user_id, {})
        user_email = user_profile.get("profile", {}).get("email")
        return await _get_notion_assignee_id(user_email)

    # 데이터 소스에서 실제 옵션들을 가져와서 동적으로 스키마 구성
    task_type_options = get_task_type_options(notion, data_source_id)
    component_options = get_component_options(notion, data_source_id)

    active_projects: dict[str, str] = {}
    if project_data_source_id:
        active_projects = get_active_projects(notion, project_data_source_id)
    project_names = list(active_projects.keys())

    # 대상 DB에 존재하는 속성만 입력 스키마에 포함
    fields: dict = {
        "title": (str, Field(description="작업의 제목")),
    }

    if task_type_options:
        fields["task_type"] = (
            str,
            Field(
                description=f"작업의 유형. 가능한 값: {', '.join(task_type_options)}",
                json_schema_extra={"enum": task_type_options},
            ),
        )

    if component_options:
        fields["component"] = (
            str,
            Field(
                description=f"작업의 구성요소. 가능한 값: {', '.join(component_options)}",
                json_schema_extra={"enum": component_options},
            ),
        )

    if project_names:
        fields["project"] = (
            str,
            Field(
                description=f"작업이 속한 프로젝트. 가능한 값: {', '.join(project_names)}",
                json_schema_extra={"enum": project_names},
            ),
        )

    fields["blocks"] = (
        str | None,
        Field(
            default=None,
            description=(
                "작업 본문을 구성할 마크다운 형식의 문자열. 다음과 같은 템플릿을 활용하라.\n"
                "# 슬랙 대화 요약\n"
                "_슬랙 대화 내용을 요약하여 작성한다._\n"
                "# 기획\n"
                "_작업 배경, 요구 사항 등을 정리하여 작성한다._\n"
                "# 의견\n"
                "_담당 엔지니어에게 전달하고 싶은 추가적인 조언. 주로 작업을 해결하기 위한 기술적 방향을 제시._\n"
                "\n"
                # 서로 다른 타입의 리스트 중첩 금지는 md2notion 의 버그
                "**마크다운 작성 제약 사항 (반드시 준수):**\n"
                "- 서로 다른 타입의 리스트 중첩 절대 금지\n"
                "- 잘못된 예 1: '1) 번호 항목\\n   - 불릿 하위 항목' (번호+불릿 혼합)\n"
                "- 잘못된 예 2: '- 불릿 항목\\n  1. 번호 하위 항목' (불릿+번호 혼합)\n"
                "- 올바른 예 1: '- 불릿\\n  - 불릿 하위' (같은 타입 중첩 OK)\n"
                "- 올바른 예 2: '1. 번호\\n   1) 번호 하위' (같은 타입 중첩 OK)\n"
                "- 올바른 예 3: 중첩 대신 별도 섹션으로 분리\n"
            ),
        ),
    )

    CreateNotionTaskInput = create_model("CreateNotionTaskInput", **fields)

    @tool("create_notion_task", args_schema=CreateNotionTaskInput)
    async def create_notion_task(
        title: str,
        task_type: str | None = None,
        component: str | None = None,
        project: str | None = None,
        blocks: str | None = None,
    ) -> str:
        """
        노션에 새로운 작업 페이지를 생성합니다.
        후속 작업 생성이 요청될 때는 후속 작업 생성 도구를 사용합니다.
        본 도구는 주로 슬랙 대화를 정리하여 노션 작업을 생성할 때 요청됩니다.

        Returns:
            생성된 노션 페이지의 URL
        """
        notion_assignee_id = await get_assignee_id()

        properties = {
            title_property_name: {"title": [{"text": {"content": title}}]},
        }
        if status_property_name and default_status:
            properties[status_property_name] = {"status": {"name": default_status}}

        if task_type and task_type_options:
            properties["유형"] = {"select": {"name": task_type}}
        if component and component_options:
            properties["구성요소"] = {"multi_select": [{"name": component}]}
        if project and active_projects:
            properties["프로젝트"] = {"relation": [{"id": active_projects[project]}]}

        if notion_assignee_id:
            properties["담당자"] = {"people": [{"id": notion_assignee_id}]}

        # Slack thread URL에서 채널 ID를 추출하여 아이디어 뱅크 채널인지 확인
        if slack_thread_url and "/archives/" in slack_thread_url:
            # URL 형식: https://monolith-keb2010.slack.com/archives/C03U6N87RKN/p1234567890
            channel_id = slack_thread_url.split("/archives/")[1].split("/")[0]
            if channel_id == "C03U6N87RKN":
                properties["아이디어 뱅크"] = {"checkbox": True}

        response = notion.pages.create(
            parent={"data_source_id": data_source_id}, properties=properties
        )

        page_id = response["id"]

        if slack_thread_url:
            notion.blocks.children.append(
                block_id=page_id,
                children=[{"type": "bookmark", "bookmark": {"url": slack_thread_url}}],
            )

        if blocks:
            for block in parse_md(blocks):
                flattened_block = flatten_deep_children(block)
                notion.blocks.children.append(page_id, children=[flattened_block])

            template = """# 작업 내용
-
# 검증

            """
            for block in parse_md(template):
                flattened_block = flatten_deep_children(block)
                notion.blocks.children.append(page_id, children=[flattened_block])

        return response["url"]

    return create_notion_task


def get_update_notion_task_deadline_tool():
    """노션 작업 마감일 업데이트 도구를 반환합니다."""

    @tool
    def update_notion_task_deadline(
        page_id: Annotated[
            str, "노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"
        ],
        new_deadline: Annotated[str, "'YYYY-MM-DD' 형태의 문자열"],
    ):
        """
        노션 작업의 타임라인을 변경합니다.
        주로 노션 작업에 대한 기한, 마감 일자 변경이 요청될 때 쓰입니다.
        """
        page_data = notion.pages.retrieve(page_id)

        old_start = None
        timeline_property = page_data["properties"].get("타임라인", {})
        date_value = timeline_property.get("date", {})

        if date_value:
            old_start = date_value.get("start")

            if old_start:
                new_start = old_start
            else:
                new_start = new_deadline
        else:
            new_start = new_deadline

        new_end = new_deadline

        notion.pages.update(
            page_id=page_id,
            properties={"타임라인": {"date": {"start": new_start, "end": new_end}}},
        )

    return update_notion_task_deadline


def get_update_notion_task_status_tool(data_source_id: str):
    """노션 작업 상태 업데이트 도구를 반환합니다."""

    status_prop_name, _ = get_status_property_info(notion, data_source_id)

    # 데이터 소스에서 실제 상태 옵션들을 가져와서 Pydantic 모델 생성
    status_options = get_status_options(notion, data_source_id)

    # 동적으로 Field 생성하여 enum constraint 추가
    status_field = Field(
        description=f"새로운 상태명. 가능한 값: {', '.join(status_options)}",
        json_schema_extra={"enum": status_options},
    )

    class UpdateNotionTaskStatusInput(BaseModel):
        page_id: str = Field(
            description="노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"
        )
        new_status: str = status_field

    @tool("update_notion_task_status", args_schema=UpdateNotionTaskStatusInput)
    def update_notion_task_status(page_id: str, new_status: str) -> None:
        """
        노션 작업의 상태를 변경합니다.
        주로 노션 작업을 진행 중, 완료, 중단 등으로 변경할 때 쓰입니다.
        상태 옵션은 실제 노션 데이터베이스에서 동적으로 가져옵니다.
        """
        notion.pages.update(
            page_id=page_id,
            properties={status_prop_name: {"status": {"name": new_status}}},
        )

    return update_notion_task_status


def get_notion_page_tool():
    """노션 페이지 조회 도구를 반환합니다."""

    @tool
    def get_notion_page(
        page_id: Annotated[
            str, "노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"
        ],
    ) -> str:
        """
        노션 페이지를 마크다운 형태로 조회합니다.
        www.notion.so 에 대한 링크는 반드시 이 도구를 사용하여 조회합니다.
        """
        return notion_page_to_markdown(page_id)

    return get_notion_page


def get_create_notion_follow_up_task_tool(data_source_id: str):
    """노션 후속 작업 생성 도구를 반환합니다."""

    component_options = get_component_options(notion, data_source_id)

    class CreateNotionFollowUpTaskInput(BaseModel):
        parent_page_id: str = Field(
            description="선행 작업의 노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"
        )
        component: str = Field(
            description=f"후속 작업의 구성요소. 가능한 값: {', '.join(component_options)}",
            json_schema_extra={"enum": component_options},
        )

    @tool("create_notion_follow_up_task", args_schema=CreateNotionFollowUpTaskInput)
    def create_notion_follow_up_task(parent_page_id: str, component: str) -> str:
        """
        선행 작업(parent_page_id)에 대하여 후속 작업을 생성합니다.
        특정 구성 요소에 대해서 생성될 수 있습니다.

        후속 작업 생성이 요청되면 create_notion_task 대신 반드시 이 도구를 사용합니다.

        Returns:
            생성된 노션 페이지의 URL
        """
        parent_page_data = notion.pages.retrieve(parent_page_id)

        parent_title = parent_page_data["properties"]["제목"]["title"][0]["text"][
            "content"
        ]
        parent_component = parent_page_data["properties"]["구성요소"]["multi_select"][
            0
        ]["name"]

        if parent_title.endswith(f" - {parent_component}"):
            title = parent_title.replace(f" - {parent_component}", f" - {component}")
        else:
            title = f"{parent_title} - {component}"

        properties = {
            "제목": {"title": [{"text": {"content": title}}]},
            "유형": {"select": {"name": "작업 🔨"}},
            "구성요소": {"multi_select": [{"name": component}]},
            "상태": {"status": {"name": "대기"}},
            "선행 작업": {"relation": [{"id": parent_page_id}]},
        }

        if parent_page_data["properties"]["프로젝트"]["relation"]:
            properties["프로젝트"] = {
                "relation": [
                    {
                        "id": parent_page_data["properties"]["프로젝트"]["relation"][0][
                            "id"
                        ]
                    }
                ]
            }

        response = notion.pages.create(
            parent={"data_source_id": data_source_id}, properties=properties
        )

        return response["url"]

    return create_notion_follow_up_task


async def answer(
    thread_ts: str,
    channel: str,
    user: str | None,
    text: str,
    say,
    client,
    tools: list,
):
    """
    슬랙에서 질문을 받아 답변을 생성하여 슬랙에 전송한다.

    Args:
        thread_ts: 스레드 타임스탬프
        channel: 채널 ID
        user: 사용자 ID. 워크플로우가 생성한 메세지면 None.
        text: 질문 내용
        say: 메시지 전송 함수
        client: 슬랙 클라이언트
        tools: 외부에서 주입할 도구들의 리스트.

    Returns:
        None
    """
    # 스레드의 모든 메시지를 가져옴
    result = await client.conversations_replies(channel=channel, ts=thread_ts)

    # 메시지에서 사용자 ID를 수집
    user_ids = set(
        message["user"] for message in result["messages"] if "user" in message
    )
    if user:
        user_ids.add(user)

    # 사용자 정보 일괄 조회
    user_info_list = await slack_users_list(client)
    user_dict = {
        user["id"]: user for user in user_info_list["members"] if user["id"] in user_ids
    }

    today_str = datetime.now(tz=KST).strftime("%Y-%m-%d(%A)")

    messages: list[BaseMessage] = [
        SystemMessage(
            content=(
                "Context:\n"
                "- You are a helpful assistant who is integrated in Slack.\n"
                "  Your answer will be sent to the Slack thread.\n"
                "  Therefore, for normal conversations, you don't have to use Slack Tool.\n"
                "- We are a edu-tech startup in Korea. So always answer in Korean.\n"
                f"- Today's date is {today_str}\n"
                "\n"
                "**슬랙 텍스트 포맷팅**:\n"
                "- 슬랙은 마크다운이 아닌 자체 mrkdwn 포맷을 사용합니다.\n"
                "- Bold: `*텍스트*` (별표 1개, **텍스트** 형식은 작동하지 않음)\n"
                "- Italic: `_텍스트_` (언더스코어)\n"
                "- Strikethrough: `~텍스트~` (물결표)\n"
                "- Code: `` `코드` `` (백틱)\n"
                "- Code block: ``` ```코드 블록``` ``` (백틱 3개)"
            )
        )
    ]

    threads = []
    for message in result["messages"][:-1]:
        slack_user_id = message.get("user", None)
        if slack_user_id:
            user_profile = user_dict.get(slack_user_id, {})
            user_real_name = user_profile.get("real_name", "Unknown")
        else:
            user_real_name = "Bot"
        threads.append(f"{user_real_name}:\n{message['text']}")

    # 최종 질의한 사용자 정보
    slack_user_id = user
    user_profile = user_dict.get(slack_user_id, {})
    user_real_name = user_profile.get("real_name", "Unknown")

    threads_joined = "\n\n".join(threads)
    messages.append(
        HumanMessage(
            content=(
                f"{threads_joined}\n"
                f"위는 슬랙에서 진행된 대화이다. {user_real_name}이(가) 위 대화에 기반하여 질문함.\n"
                f"{text}\n"
            )
        )
    )

    # Slack 스레드 링크 만들기
    # Slack 메시지 링크 형식: https://<workspace>.slack.com/archives/<channel_id>/p<message_ts>
    # thread_ts는 보통 소수점 형태 ex) 1690891234.123456이므로 '.' 제거
    slack_workspace = "monolith-keb2010"  # 실제 워크스페이스 도메인으로 변경 필요
    thread_ts_for_link = thread_ts.replace(".", "")
    slack_thread_url = (
        f"https://{slack_workspace}.slack.com"
        f"/archives/{channel}/p{thread_ts_for_link}"
    )

    user_email = user_profile.get("profile", {}).get("email")

    if text.startswith("o3"):
        model = "o3"
    else:
        model = "gpt-5.2"

    chat_model = ChatOpenAI(model=model)
    agent_executor = create_react_agent(chat_model, tools, debug=True)

    # 툴 호출 상태를 슬랙에 표시하는 핸들러
    tool_status_handler = ToolStatusHandler(
        say=say, thread_ts=thread_ts, slack_client=client, channel=channel
    )

    response = await agent_executor.ainvoke(
        {"messages": messages},
        {"callbacks": [tool_status_handler], "recursion_limit": 50},
    )

    agent_answer = response["messages"][-1].content

    await say(
        {
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": agent_answer}}
            ]
        },
        thread_ts=thread_ts,
    )
