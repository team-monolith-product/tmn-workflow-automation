"""
공통 유틸리티 함수들
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Literal

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


def get_notion_tools(notion_assignee_id: str | None, slack_thread_url: str):
    """
    노션 관련 도구들을 생성하여 반환합니다.
    """

    @tool
    def create_notion_task(
        title: Annotated[str, "작업의 제목"],
        task_type: Annotated[Literal["작업 🔨", "버그 🐞"], "작업의 유형"],
        component: Annotated[
            Literal["기획", "디자인", "프론트", "백", "인프라", "데이터", "AI"],
            "작업의 구성요소",
        ],
        project: Annotated[
            Literal["유지보수", "기술개선", "경험개선", "오픈소스"],
            "작업이 속한 프로젝트",
        ],
        blocks: Annotated[
            str | None,
            (
                "작업 본문을 구성할 마크다운 형식의 문자열. 다음과 같은 템플릿을 활용하라.\n"
                "# 슬랙 대화 요약\n"
                "_슬랙 대화 내용을 요약하여 작성한다._\n"
                "# 기획\n"
                "_작업 배경, 요구 사항 등을 정리하여 작성한다._\n"
                "# 의견\n"
                "_담당 엔지니어에게 전달하고 싶은 추가적인 조언. 주로 작업을 해결하기 위한 기술적 방향을 제시._\n"
            ),
        ],
    ) -> str:
        """
        노션에 새로운 작업 페이지를 생성합니다.
        후속 작업 생성이 요청될 때는 후속 작업 생성 도구를 사용합니다.
        본 도구는 주로 슬랙 대화를 정리하여 노션 작업을 생성할 때 요청됩니다.

        Returns:
            생성된 노션 페이지의 URL
        """
        properties = {
            "제목": {"title": [{"text": {"content": title}}]},
            "유형": {"select": {"name": task_type}},
            "구성요소": {"multi_select": [{"name": component}]},
            "프로젝트": {"relation": [{"id": PROJECT_TO_PAGE_ID[project]}]},
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

    @tool
    def update_notion_task_status(
        page_id: Annotated[
            str, "노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"
        ],
        new_status: Annotated[
            Literal["대기", "진행", "리뷰", "완료", "중단"], "새로운 상태명"
        ],
    ):
        """
        노션 작업의 상태를 변경합니다.
        주로 노션 작업을 진행 중, 완료, 중단 등으로 변경할 때 쓰입니다.
        """
        notion.pages.update(
            page_id=page_id, properties={"상태": {"status": {"name": new_status}}}
        )

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

    @tool
    def create_notion_follow_up_task(
        parent_page_id: Annotated[
            str, "선행 작업의 노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"
        ],
        component: Annotated[
            Literal["디자인", "프론트", "백", "인프라", "데이터", "AI"],
            "후속 작업의 구성요소",
        ],
    ) -> str:
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
            parent={"database_id": DATABASE_ID}, properties=properties
        )

        return response["url"]

    return [
        create_notion_task,
        update_notion_task_deadline,
        update_notion_task_status,
        create_notion_follow_up_task,
        get_notion_page,
    ]


async def answer(
    thread_ts: str,
    channel: str,
    user: str | None,
    text: str,
    say,
    client,
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
    notion_users = await notion_users_list(notion)

    # 이메일이 slack_email인 Notion 사용자 찾기
    matched_notion_user = next(
        (
            user
            for user in notion_users["results"]
            if user["type"] == "person" and user["person"]["email"] == user_email
        ),
        None,
    )

    notion_assignee_id = matched_notion_user["id"] if matched_notion_user else None

    notion_tools = get_notion_tools(notion_assignee_id, slack_thread_url)
    
    tools = [
        search_tool,
        get_web_page_from_url
    ] + notion_tools

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
