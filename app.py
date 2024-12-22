import os
from datetime import datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from openai import OpenAI
from notion_client import Client as NotionClient
import json

# 환경 변수 로드
load_dotenv()

client = OpenAI()

# 노션 클라이언트 초기화
notion = NotionClient(auth=os.environ.get("NOTION_API_KEY"))
DATABASE_ID: str = 'a9de18b3877c453a8e163c2ee1ff4137'

PROJECT_TO_PAGE_ID = {
    "유지보수": "12d1cc820da680c7ae8cdd40b5667798",
    "기술개선": "12d1cc820da68060b803eb9c0904e40c",
    "경험개선": "12d1cc820da68005a4b4fdb6f7221ff3",
    "오픈소스": "2a17626c85574a958fb584f2fb2eda08"
}


def create_notion_task(title, task_type, component, project, thread_url):
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
            }
        }
    )

    page_id = response["id"]

    # 페이지에 Slack 스레드 링크 추가 (bookmark 블록)
    if thread_url:
        notion.blocks.children.append(
            block_id=page_id,
            children=[
                {
                    "type": "bookmark",
                    "bookmark": {
                        "url": thread_url
                    }
                }
            ]
        )

    return response["url"]


def update_notion_task_deadline(page_id: str, new_deadline: str):
    """
    기존 노션 페이지의 종료일(date)을 업데이트한다.
    page_id: 노션 페이지 ID (ex: '12d1cc82...')
    new_deadline: 'YYYY-MM-DD' 형태의 문자열
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


# OpenAI 함수 정의
functions = [
    {
        "name": "create_notion_task",
        "description": "노션에 새로운 과업을 생성합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "과업의 제목"
                },
                "task_type": {
                    "type": "string",
                    "enum": ["작업 🔨", "버그 🐞"],
                    "description": "과업의 유형"
                },
                "component": {
                    "type": "string",
                    "enum": ["Front", "Back", "Infra", "Data", "Plan", "AI"],
                    "description": "과업의 구성요소"
                },
                "project": {
                    "type": "string",
                    "enum": ["유지보수", "기술개선", "경험개선", "오픈소스"],
                    "description": "과업이 속한 프로젝트"
                },
            },
            "required": ["title", "task_type", "component"]
        }
    },
    {
        "name": "update_notion_task_deadline",
        "description": "노션 과업의 타입라인의 종료일을 업데이트합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "노션 페이지 ID (ex: '12d1cc82...')"
                },
                "new_deadline": {
                    "type": "string",
                    "description": "새로운 기한 (YYYY-MM-DD 포맷)"
                }
            },
            "required": ["task_id", "new_deadline"]
        }
    }
]

# Initializes your app with your bot token and socket mode handler
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))


@app.event("app_mention")
def event_test(body, say, logger):
    thread_ts = body.get("event", {}).get("thread_ts") or body["event"]["ts"]
    channel = body["event"]["channel"]

    # 스레드의 모든 메시지를 가져옴
    result = app.client.conversations_replies(
        channel=channel,
        ts=thread_ts
    )

    # 메시지에서 사용자 ID를 수집
    user_ids = set(message["user"] for message in result["messages"])
    user_ids.add(body["event"]["user"])

    # 사용자 정보 일괄 조회
    user_info_list = app.client.users_list()
    user_dict = {
        user["id"]: user["real_name"]
        for user in user_info_list["members"]
        if user["id"] in user_ids
    }

    today_str = datetime.now().strftime('%Y-%m-%d(%A)')
    messages = [{
        "role": "system",
        "content": f"You are a helpful assistant who is integrated in Slack. "
                f"We are a edu-tech startup in Korea. Always answer in Korean. "
                f"Today's date is {today_str}"
    }]

    threads = []
    for message in result["messages"]:
        user_name = user_dict.get(message["user"], "Unknown")
        threads.append(f"{user_name}:\n{message['text']}")

    user_name = user_dict.get(body["event"]["user"], "Unknown")
    threads_joined = '\n\n'.join(threads)
    messages.append({
        "role": "user",
        "content": f"{threads_joined}\n위는 슬랙에서 진행된 대화이다. {user_name}이(가) 너에게 위 대화에 기반하여 다음과 같이 질문하니 답변하여라.\n{body['event']['text']}"
    })

    # Slack 스레드 링크 만들기
    # Slack 메시지 링크 형식: https://<workspace>.slack.com/archives/<channel_id>/p<message_ts>
    # thread_ts는 보통 소수점 형태 ex) 1690891234.123456이므로 '.' 제거
    slack_workspace = "monolith-keb2010"  # 실제 워크스페이스 도메인으로 변경 필요
    thread_ts_for_link = thread_ts.replace('.', '')
    slack_thread_url = f"https://{slack_workspace}.slack.com/archives/{channel}/p{thread_ts_for_link}"

    chat_completion = client.chat.completions.create(
        messages=messages,
        model="gpt-4o",
        functions=functions,
        function_call="auto"
    )

    response_message = chat_completion.choices[0].message

    if response_message.function_call:
        function_name = response_message.function_call.name
        arguments = json.loads(response_message.function_call.arguments)

        if function_name == "create_notion_task":
            task_url = create_notion_task(
                title=arguments.get("title"),
                task_type=arguments.get("task_type"),
                component=arguments.get("component"),
                project=arguments.get("project"),
                thread_url=slack_thread_url
            )
            say(f"노션에 과업 '{arguments.get('title')}'이 생성되었습니다.\n링크: {task_url}",
                thread_ts=thread_ts)
        elif function_name == "update_notion_task_deadline":
            # 새로 추가된 로직
            notion_page_id = arguments.get("task_id")
            new_deadline = arguments.get("new_deadline")

            # 실제 Notion 과업의 기한 업데이트
            update_notion_task_deadline(notion_page_id, new_deadline)

            # 사용자에게 완료 메시지
            say(f"과업의 기한을 {new_deadline}로 업데이트했습니다.", thread_ts=thread_ts)
    else:
        say(response_message.content, thread_ts=thread_ts)


# Start your app
if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
