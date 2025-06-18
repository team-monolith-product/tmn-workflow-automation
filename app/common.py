"""
공통 유틸리티 함수들
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Literal
from pydantic import BaseModel, Field

from cachetools import TTLCache
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from notion2md.exporter.block import StringExporter
from langchain_core.tools import tool
from langchain_community.document_loaders import WebBaseLoader
from langchain_community.tools import TavilySearchResults
from slack_sdk.web.async_client import AsyncWebClient
from md2notionpage.core import parse_md

# 환경 변수 로드
load_dotenv()

# 시간대 설정
KST = ZoneInfo("Asia/Seoul")

# 노션 클라이언트 초기화
notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))
DATABASE_ID: str = "a9de18b3877c453a8e163c2ee1ff4137"
PROJECT_TO_PAGE_ID = {
    "유지보수": "1dd1cc820da6805db022fb396e959a44",
    "기술개선": "1dd1cc820da680ef9763cb5526f142cf",
    "경험개선": "1dd1cc820da680fdb25dc9e3cd387cba",
    "오픈소스": "2a17626c85574a958fb584f2fb2eda08",
}

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


def get_database_schema(client: NotionClient, database_id: str):
    """
    노션 데이터베이스 스키마를 조회한다.
    """
    cache_key = f"database_schema_{database_id}"
    if cache_key in _cache_database_schema:
        return _cache_database_schema[cache_key]

    resp = client.databases.retrieve(database_id)
    _cache_database_schema[cache_key] = resp
    return resp


def get_status_options(client: NotionClient, database_id: str) -> list[str]:
    """
    노션 데이터베이스에서 상태 속성의 가능한 옵션들을 조회한다.
    """
    db_schema = get_database_schema(client, database_id)
    status_property = db_schema["properties"].get("상태", {})

    if "status" in status_property:
        options = status_property["status"].get("options", [])
        return [option["name"] for option in options]

    return []


def get_task_type_options(client: NotionClient, database_id: str) -> list[str]:
    """
    노션 데이터베이스에서 유형 속성의 가능한 옵션들을 조회한다.
    """
    db_schema = get_database_schema(client, database_id)
    task_type_property = db_schema["properties"].get("유형", {})

    if "select" in task_type_property:
        options = task_type_property["select"].get("options", [])
        return [option["name"] for option in options]

    return []


def get_component_options(client: NotionClient, database_id: str) -> list[str]:
    """
    노션 데이터베이스에서 구성요소 속성의 가능한 옵션들을 조회한다.
    """
    db_schema = get_database_schema(client, database_id)
    component_property = db_schema["properties"].get("구성요소", {})

    if "multi_select" in component_property:
        options = component_property["multi_select"].get("options", [])
        return [option["name"] for option in options]

    return []


def get_active_projects(client: NotionClient, project_db_id: str) -> dict[str, str]:
    """프로젝트 DB에서 '진행 중' 상태인 프로젝트들을 조회하여 프로젝트명:페이지ID 매핑을 반환합니다."""
    response = client.databases.query(
        database_id=project_db_id,
        filter={"property": "상태", "status": {"equals": "진행 중"}},
    )

    project_mapping = {}
    for page in response["results"]:
        # 페이지 제목 가져오기 (일반적으로 '이름' 또는 '제목' 속성)
        title_property = None
        for prop_name, prop_value in page["properties"].items():
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
    database_id: str,
    client,
    project_db_id: str,
):
    """노션 작업 생성 도구를 반환합니다."""

    async def get_assignee_id():
        if user_id is None:
            return None
        # 사용자 정보 조회
        user_info_list = await slack_users_list(client)
        user_dict = {user["id"]: user for user in user_info_list["members"]}
        user_profile = user_dict.get(user_id, {})
        user_email = user_profile.get("profile", {}).get("email")
        return await _get_notion_assignee_id(user_email)

    # 데이터베이스에서 실제 옵션들을 가져와서 Pydantic 모델 생성
    task_type_options = get_task_type_options(notion, database_id)
    component_options = get_component_options(notion, database_id)

    # 프로젝트 DB에서 진행 중인 프로젝트들 조회
    active_projects = get_active_projects(notion, project_db_id)
    project_names = list(active_projects.keys())

    # 동적으로 Field 생성하여 enum constraint 추가
    task_type_field = Field(
        description=f"작업의 유형. 가능한 값: {', '.join(task_type_options)}",
        json_schema_extra={"enum": task_type_options},
    )

    component_field = Field(
        description=f"작업의 구성요소. 가능한 값: {', '.join(component_options)}",
        json_schema_extra={"enum": component_options},
    )

    class CreateNotionTaskInput(BaseModel):
        title: str = Field(description="작업의 제목")
        task_type: str = task_type_field
        component: str = component_field
        project: str = Field(
            description=f"작업이 속한 프로젝트. 가능한 값: {', '.join(project_names)}",
            json_schema_extra={"enum": project_names},
        )
        blocks: str | None = Field(
            default=None,
            description=(
                "작업 본문을 구성할 마크다운 형식의 문자열. 다음과 같은 템플릿을 활용하라.\n"
                "# 슬랙 대화 요약\n"
                "_슬랙 대화 내용을 요약하여 작성한다._\n"
                "# 기획\n"
                "_작업 배경, 요구 사항 등을 정리하여 작성한다._\n"
                "# 의견\n"
                "_담당 엔지니어에게 전달하고 싶은 추가적인 조언. 주로 작업을 해결하기 위한 기술적 방향을 제시._\n"
            ),
        )

    @tool("create_notion_task", args_schema=CreateNotionTaskInput)
    async def create_notion_task(
        title: str,
        task_type: str,
        component: str,
        project: str,
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
            "제목": {"title": [{"text": {"content": title}}]},
            "유형": {"select": {"name": task_type}},
            "구성요소": {"multi_select": [{"name": component}]},
            "프로젝트": {"relation": [{"id": active_projects[project]}]},
            "상태": {"status": {"name": "대기"}},
        }

        if notion_assignee_id:
            properties["담당자"] = {"people": [{"id": notion_assignee_id}]}

        response = notion.pages.create(
            parent={"database_id": DATABASE_ID}, properties=properties
        )

        page_id = response["id"]

        if slack_thread_url:
            notion.blocks.children.append(
                block_id=page_id,
                children=[{"type": "bookmark", "bookmark": {"url": slack_thread_url}}],
            )

        if blocks:
            for block in parse_md(blocks):
                notion.blocks.children.append(page_id, children=[block])

            template = """# 작업 내용
- 
# 검증

            """
            for block in parse_md(template):
                notion.blocks.children.append(page_id, children=[block])

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


def get_update_notion_task_status_tool(database_id: str):
    """노션 작업 상태 업데이트 도구를 반환합니다."""

    # 데이터베이스에서 실제 상태 옵션들을 가져와서 Pydantic 모델 생성
    status_options = get_status_options(notion, database_id)

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
            page_id=page_id, properties={"상태": {"status": {"name": new_status}}}
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
        return StringExporter(block_id=page_id, output_path="test").export()

    return get_notion_page


def get_create_notion_follow_up_task_tool(database_id: str):
    """노션 후속 작업 생성 도구를 반환합니다."""

    component_options = get_component_options(notion, database_id)

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
            parent={"database_id": database_id}, properties=properties
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
                f"- Today's date is {today_str}"
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
        model = "gpt-4.1"

    chat_model = ChatOpenAI(model=model)
    agent_executor = create_react_agent(chat_model, tools, debug=True)

    class SayHandler(BaseCallbackHandler):
        """
        Agent Handler That Slack-Says the Tool Call
        """

        async def on_tool_start(
            self,
            serialized,
            input_str,
            **kwargs,
        ):
            await say(
                {
                    "blocks": [
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "plain_text",
                                    "text": f"{serialized['name']}({input_str}) 실행 중...",
                                }
                            ],
                        }
                    ]
                },
                thread_ts=thread_ts,
            )

    response = await agent_executor.ainvoke(
        {"messages": messages}, {"callbacks": [SayHandler()]}
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
