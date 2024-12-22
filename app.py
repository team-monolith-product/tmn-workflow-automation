import os
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

    messages = [{
        "role": "system",
        "content": "You are a helpful assistant who is integrated in Slack. We are a edu-tech startup in Korea. Always answer in Korean."
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
            say(f"노션에 과업 '{arguments.get('title')}'이 생성되었습니다.\n링크: {task_url}", thread_ts=thread_ts)
    else:
        say(response_message.content, thread_ts=thread_ts)


# Start your app
if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
