"""
슬랙에서 로봇을 멘션하여 답변을 얻고, 노션에 작업을 생성하거나 업데이트하는 기능을 제공하는 슬랙 봇입니다.
"""
from datetime import datetime, timedelta
import logging
import os
from typing import Annotated, Literal

from cachetools import cached, TTLCache
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from notion2md.exporter.block import StringExporter
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_core.tools import tool
from langchain_community.agent_toolkits import SlackToolkit
from langchain_community.document_loaders import WebBaseLoader
from langchain_community.tools import TavilySearchResults
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from slack_bolt import App, Assistant, BoltContext, SetStatus, Say
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from md2notionpage.core import parse_md

# 환경 변수 로드
load_dotenv()

# 노션 클라이언트 초기화
notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))
DATABASE_ID: str = 'a9de18b3877c453a8e163c2ee1ff4137'
PROJECT_TO_PAGE_ID = {
    "유지보수": "16f1cc820da68045a972c1da9a72f335",
    "기술개선": "16f1cc820da680c99d35dde36ad2f7f2",
    "경험개선": "16f1cc820da6809fb2d3dc7f91401c1d",
    "오픈소스": "2a17626c85574a958fb584f2fb2eda08"
}


@cached(TTLCache(maxsize=100, ttl=3600))
def slack_users_list(client: WebClient):
    """
    슬랙 사용자 목록을 조회한다.
    """
    return client.users_list()


@cached(TTLCache(maxsize=100, ttl=3600))
def notion_users_list(client: NotionClient):
    """
    노션 사용자 목록을 조회한다.
    """
    return client.users.list()


# Initializes your app with your bot token and socket mode handler
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
assistant = Assistant()

# SlackToolkit이 요구함.
os.environ["SLACK_USER_TOKEN"] = os.environ.get("SLACK_BOT_TOKEN")

search_tool = TavilySearchResults(
    max_results=10,
    search_depth="advanced",
    include_answer=True,
    include_raw_content=True,
    include_images=False,
    # include_domains=[...],
    # exclude_domains=[...],
    # name="...",            # overwrite default tool name
    # description="...",     # overwrite default tool description
    # args_schema=...,       # overwrite default args_schema: BaseModel
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


def answer(
    thread_ts,
    channel, 
    user, 
    text, 
    say
):
    """
    
    """
    # 스레드의 모든 메시지를 가져옴
    result = app.client.conversations_replies(
        channel=channel,
        ts=thread_ts
    )

    # 메시지에서 사용자 ID를 수집
    user_ids = set(message["user"]
                   for message in result["messages"] if "user" in message)
    user_ids.add(user)

    # 사용자 정보 일괄 조회
    user_info_list = slack_users_list(app.client)
    user_dict = {
        user["id"]: user for user in user_info_list["members"]
        if user["id"] in user_ids
    }

    today_str = datetime.now().strftime('%Y-%m-%d(%A)')

    if text.startswith("o1"):
        model = "o1"
    else:
        model = "gpt-4o"

    chat_model = ChatOpenAI(model=model)

    messages: list[BaseMessage] = [SystemMessage(content=(
        f"You are a helpful assistant who is integrated in Slack. "
        f"We are a edu-tech startup in Korea. Always answer in Korean. "
        f"Today's date is {today_str}"
    ))]

    threads = []
    for message in result["messages"]:
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
    messages.append(HumanMessage(
        content=(
            f"{threads_joined}\n"
            f"위는 슬랙에서 진행된 대화이다. {user_real_name}이(가) 위 대화에 기반하여 질문함.\n"
            f"{text}\n"
        )
    ))

    # Slack 스레드 링크 만들기
    # Slack 메시지 링크 형식: https://<workspace>.slack.com/archives/<channel_id>/p<message_ts>
    # thread_ts는 보통 소수점 형태 ex) 1690891234.123456이므로 '.' 제거
    slack_workspace = "monolith-keb2010"  # 실제 워크스페이스 도메인으로 변경 필요
    thread_ts_for_link = thread_ts.replace('.', '')
    slack_thread_url = (f"https://{slack_workspace}.slack.com"
                        f"/archives/{channel}/p{thread_ts_for_link}")

    user_email = user_profile.get("profile", {}).get("email")

    notion_users = notion_users_list(notion)

    # 이메일이 slack_email인 Notion 사용자 찾기
    matched_notion_user = next(
        (
            user for user in notion_users["results"]
            if user["type"] == "person" and user["person"]["email"] == user_email
        ),
        None
    )

    notion_assignee_id = matched_notion_user["id"] if matched_notion_user else None

    @tool
    def create_notion_task(
        title: Annotated[str, "작업의 제목"],
        task_type: Annotated[Literal["작업 🔨", "버그 🐞"], "작업의 유형"],
        component: Annotated[Literal["기획", "디자인", "프론트", "백", "인프라", "데이터", "AI"], "작업의 구성요소"],
        project: Annotated[Literal["유지보수", "기술개선", "경험개선", "오픈소스"], "작업이 속한 프로젝트"],
        blocks: Annotated[str | None, (
            "작업 본문을 구성할 마크다운 형식의 문자열. 다음과 같은 템플릿을 활용하라.\n"
            "# 슬랙 대화 요약\n"
            "_슬랙 대화 내용을 요약하여 작성한다._\n"
            "# 기획\n"
            "_작업 배경, 요구 사항 등을 정리하여 작성한다._\n"
            "# 의견\n"
            "_담당 엔지니어에게 전달하고 싶은 추가적인 조언. 주로 작업을 해결하기 위한 기술적 방향을 제시._\n"
        )]
    ) -> str:
        """
        노션에 새로운 작업 페이지를 생성합니다.
        후속 작업 생성이 요청될 때는 후속 작업 생성 도구를 사용합니다.
        본 도구는 주로 슬랙 대화를 정리하여 노션 작업을 생성할 때 요청됩니다.

        Returns:
            생성된 노션 페이지의 URL
        """
        response = notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={
                "제목": {
                    "title": [
                        {
                            "text": {
                                "content": title
                            }
                        }
                    ]
                },
                "유형": {
                    "select": {
                        "name": task_type
                    }
                },
                "구성요소": {
                    "multi_select": [
                        {
                            "name": component
                        }
                    ]
                },
                "프로젝트": {
                    "relation": [
                        {
                            "id": PROJECT_TO_PAGE_ID[project]
                        }
                    ]
                },
                "상태": {
                    "status": {
                        "name": "대기"
                    }
                },
                "담당자": {
                    "people": [
                        {
                            "id": notion_assignee_id
                        }
                    ]
                }
            }
        )

        page_id = response["id"]

        # 페이지에 Slack 스레드 링크 추가 (bookmark 블록)
        if slack_thread_url:
            notion.blocks.children.append(
                block_id=page_id,
                children=[
                    {
                        "type": "bookmark",
                        "bookmark": {
                            "url": slack_thread_url
                        }
                    }
                ]
            )

        if blocks:
            for block in parse_md(blocks):
                notion.blocks.children.append(
                    page_id,
                    children=[block]
                )

            # 템플릿의 나머지 영역을 블록으로 추가
            template = """# 작업 내용

    # 검증

            """
            for block in parse_md(template):
                notion.blocks.children.append(
                    page_id,
                    children=[block]
                )

        return response["url"]

    @tool
    def update_notion_task_deadline(
        page_id: Annotated[str, "노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"],
        new_deadline: Annotated[str, "'YYYY-MM-DD' 형태의 문자열"]
    ):
        """
        노션 작업의 타임라인을 변경합니다.
        주로 노션 작업에 대한 기한, 마감 일자 변경이 요청될 때 쓰입니다.
        """
        # 1) 기존 페이지 정보 조회
        page_data = notion.pages.retrieve(page_id)

        # 2) 기존 '타임라인'의 start 값 가져오기
        #    (없는 경우 None 처리 등 분기 필요)
        old_start = None
        timeline_property = page_data["properties"].get("타임라인", {})
        date_value = timeline_property.get("date", {})
        old_start = date_value.get("start")  # 예: '2024-12-01'

        # 만약 start가 None이라면 end 업데이트가 무의미할 수도 있으므로,
        # 필요 시 분기 처리(없으면 start == end로 맞춘다던가).
        if old_start is None:
            # 예: start가 없던 경우 -> end만 존재하거나?
            # 사용 용도에 맞춰 처리
            old_start = new_deadline

        # 3) Notion 페이지 업데이트 (start는 기존값, end만 바꿔치기)
        notion.pages.update(
            page_id=page_id,
            properties={
                # 예) 속성 이름이 "종료일"인 경우
                "타임라인": {
                    "date": {
                        "start": old_start,
                        "end": new_deadline
                    }
                }
            }
        )

    @tool
    def update_notion_task_status(
        page_id: Annotated[str, "노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"],
        new_status: Annotated[Literal["대기", "진행", "리뷰", "완료", "중단"], "새로운 상태명"]
    ):
        """
        노션 작업의 상태를 변경합니다.
        주로 노션 작업을 진행 중, 완료, 중단 등으로 변경할 때 쓰입니다.
        """
        notion.pages.update(
            page_id=page_id,
            properties={
                "상태": {
                    "status": {
                        "name": new_status
                    }
                }
            }
        )

    @tool
    def get_notion_page(
        page_id: Annotated[str, "노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"],
    ) -> str:
        """
        노션 페이지를 마크다운 형태로 조회합니다.
        www.notion.so 에 대한 링크는 반드시 이 도구를 사용하여 조회합니다.
        """
        return StringExporter(block_id=page_id, output_path="test").export()

    @tool
    def create_notion_follow_up_task(
        parent_page_id: Annotated[str, "선행 작업의 노션 페이지 ID. ^[a-f0-9]{32}$ 형식. (ex: '12d1cc82...')"],
        component: Annotated[Literal["디자인", "프론트", "백", "인프라", "데이터", "AI"], "후속 작업의 구성요소"],
    ) -> str:
        """
        선행 작업(parent_page_id)에 대하여 후속 작업을 생성합니다.
        특정 구성 요소에 대해서 생성될 수 있습니다.

        후속 작업 생성이 요청되면 create_notion_task 대신 반드시 이 도구를 사용합니다.

        Returns:
            생성된 노션 페이지의 URL
        """
        parent_page_data = notion.pages.retrieve(parent_page_id)

        # 선행 작업의 제목에서 구성 요소 명을 치환하여 후속 작업 제목을 생성
        # 예)
        # "블록 코딩 에디터 디자인 개선 - 디자인" -> "블록 코딩 에디터 디자인 개선 - 프론트"
        #
        # 주로 "- {구성요소}" 로 끝나는 제목을 가정하고 작성
        parent_title = parent_page_data["properties"]["제목"]["title"][0]["text"]["content"]
        parent_component = parent_page_data["properties"]["구성요소"]["multi_select"][0]["name"]

        if parent_title.endswith(f" - {parent_component}"):
            title = parent_title.replace(
                f" - {parent_component}", f" - {component}")
        else:
            title = f"{parent_title} - {component}"

        response = notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={
                "제목": {
                    "title": [
                        {
                            "text": {
                                "content": title
                            }
                        }
                    ]
                },
                "유형": {
                    "select": {
                        "name": "작업 🔨"
                    }
                },
                "구성요소": {
                    "multi_select": [
                        {
                            "name": component
                        }
                    ]
                },
                "프로젝트": {
                    "relation": [
                        {
                            "id": parent_page_data["properties"]["프로젝트"]["relation"][0]["id"]
                        }
                    ]
                },
                "상태": {
                    "status": {
                        "name": "대기"
                    }
                },
                "선행 작업": {
                    "relation": [
                        {
                            "id": parent_page_id
                        }
                    ]
                }
            }
        )

        return response["url"]

    @tool
    def search_slack_messsages(
        query: Annotated[str, "검색어"]
    ) -> list[str]:
        """
        슬랙 메시지를 검색합니다.
        이 도구는 슬랙에 저장될 만 한 사내 지식을 검색할 때 유용합니다.

        Returns:
            검색 결과 메시지의 리스트
        """
        response = app.client.search_messages(
            query=query,
            sort="timestamp",
            sort_dir="desc",
        )

        # 로봇 메시지는 제외
        return [
            message["text"] for message in response["messages"]["matches"]
            if message.get("user", None)
        ]

    agent_executor = create_react_agent(chat_model, [
        create_notion_task,
        update_notion_task_deadline,
        update_notion_task_status,
        create_notion_follow_up_task,
        get_notion_page,
        search_tool,
        get_web_page_from_url
    ] + SlackToolkit().get_tools())

    class SayHandler(BaseCallbackHandler):
        """
        Agent Handler That Slack-Says the Tool Call
        """

        def on_tool_start(
            self,
            serialized,
            input_str,
            *,
            run_id,
            parent_run_id=None,
            tags=None,
            metadata=None,
            inputs=None,
            **kwargs,
        ):
            say(
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
                thread_ts=thread_ts
            )

    response = agent_executor.invoke(
        {"messages": messages},
        {"callbacks": [SayHandler()]}
    )
    messages = response["messages"]

    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                        "type": "mrkdwn",
                        "text": messages[-1].content
                }

            }
        ]
    }, thread_ts=thread_ts)


@app.event("app_mention")
def app_mention(body, say):
    """
    슬랙에서 로봇을 멘션하여 대화를 시작하면 호출되는 이벤트
    """
    thread_ts = body.get("event", {}).get("thread_ts") or body["event"]["ts"]
    channel = body["event"]["channel"]
    user = body["event"]["user"]
    text = body["event"]["text"]
    answer(thread_ts, channel, user, text, say)


SLACK_DAILY_SCRUM_CHANNEL_ID = 'C02JX95U7AP'
SLACK_DAILY_SCRUM_CANVAS_ID = 'F05S8Q78CGZ'

USER_ID_TO_LAST_HUDDLE_JOINED_AT = {}


@app.event("user_huddle_changed")
def user_huddle_changed(body, say):
    """
    사용자가 huddle을 변경할 때 호출되는 이벤트
    """
    event_ts = body.get("event", {}).get("event_ts")
    response = app.client.conversations_history(
        channel=SLACK_DAILY_SCRUM_CHANNEL_ID, latest=event_ts, limit=1)

    print(response)

    messages = response.data.get("messages")
    if not messages:
        return

    print(messages)

    message = messages[0]
    if not message:
        return

    print(message)

    room = message.get("room")
    if not room:
        return

    print(room)

    participants = room.get("participants")
    if not participants:
        return

    print(participants)

    # 사용자 정보 일괄 조회
    user_info_list = slack_users_list(app.client)
    user_dict = {
        user["id"]: user for user in user_info_list["members"]
    }
    for participant in participants:
        # 최근 허들 참여 시간 업데이트를 했다면 절차를 생략함.
        # 30분
        last_joined_at = USER_ID_TO_LAST_HUDDLE_JOINED_AT.get(participant)
        if last_joined_at and (datetime.now() - last_joined_at) < timedelta(minutes=30):
            # 30분 이내에 허들에 참여한 이력이 있다면 생략
            continue
        USER_ID_TO_LAST_HUDDLE_JOINED_AT[participant] = datetime.now()

        user_name = user_dict[participant]["real_name"]

        sections = app.client.canvases_sections_lookup(
            canvas_id=SLACK_DAILY_SCRUM_CANVAS_ID,
            criteria={
                "contains_text": user_dict[participant]["real_name"]
            }
        )["sections"]
        for section in sections:
            app.client.canvases_edit(
                canvas_id=SLACK_DAILY_SCRUM_CANVAS_ID,
                changes=[{
                    'operation': 'replace',
                    'section_id': section['id'],
                    'document_content': {
                        "type": "markdown",
                        "markdown": f"- [x] {user_name}\n"
                    }
                }]
            )


@assistant.thread_started
def start_assistant_thread(say, _set_suggested_prompts):
    """
    Assistant thread started
    """
    say(":wave: 안녕하세요. 무엇을 도와드릴까요?")


@assistant.user_message
def respond_in_assistant_thread(
    payload: dict,
    logger: logging.Logger,
    context: BoltContext,
    set_status: SetStatus,
    client: WebClient,
    say: Say,
):
    """
    Respond to a user message in the assistant thread.
    """
    answer(context.thread_ts, context.channel_id,
           context.user_id, payload["text"], say)


# Start your app
if __name__ == "__main__":
    app.use(assistant)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
