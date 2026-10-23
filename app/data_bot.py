"""
데이터 봇 전용 로직
"""

from datetime import datetime

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from service.llm import DEFAULT_MODEL, RESPONSES_OUTPUT_VERSION, extract_text
from .common import KST, slack_users_list
from .event_dedup import is_duplicate_event
from .tool_status_handler import ToolStatusHandler
from .tools.athena_tools import DATA_ANALYSIS_GUIDE, get_execute_athena_query_tool
from .tools.python_tools import get_execute_python_tool
from .tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)


async def collect_thread_context(
    slack_client,
    channel: str,
    thread_ts: str,
    user: str | None,
) -> tuple[str, str]:
    """
    스레드의 이전 대화 내용과 질문자 이름을 수집합니다.

    스레드의 마지막 메시지는 방금 도착한 질문이므로 대화 내용에서 제외합니다.

    Args:
        slack_client: Slack 클라이언트
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프
        user: 질문자의 Slack 사용자 ID

    Returns:
        tuple[str, str]: (질문자 이름, 이전 대화 내용)
    """
    result = await slack_client.conversations_replies(channel=channel, ts=thread_ts)

    # 메시지에서 사용자 ID를 수집
    user_ids = set(
        message["user"] for message in result["messages"] if "user" in message
    )
    if user:
        user_ids.add(user)

    # 사용자 정보 일괄 조회
    user_info_list = await slack_users_list(slack_client)
    user_dict = {
        member["id"]: member
        for member in user_info_list["members"]
        if member["id"] in user_ids
    }

    threads = []
    for message in result["messages"][:-1]:
        slack_user_id = message.get("user", None)
        if slack_user_id:
            user_profile = user_dict.get(slack_user_id, {})
            speaker_name = user_profile.get("real_name", "Unknown")
        else:
            speaker_name = "Bot"
        threads.append(f"{speaker_name}:\n{message['text']}")

    user_real_name = user_dict.get(user, {}).get("real_name", "Unknown")

    return user_real_name, "\n\n".join(threads)


def register_data_handlers(app_data):
    """
    데이터 봇의 이벤트 핸들러를 등록합니다.
    """

    @app_data.event("app_mention")
    async def app_mention_data(body, say):
        """
        슬랙에서 데이터 봇을 멘션하여 데이터 분석을 시작하면 호출되는 이벤트
        """
        if is_duplicate_event(body):
            return

        event = body.get("event")

        if event is None:
            return

        # 봇이 보낸 메시지는 무시 (자기 자신을 태그하는 무한 루프 방지)
        if event.get("bot_id"):
            return

        thread_ts = event.get("thread_ts") or body["event"]["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        user_real_name, threads_joined = await collect_thread_context(
            app_data.client, channel, thread_ts, user
        )

        await answer_data_analysis(
            thread_ts,
            channel,
            user_real_name,
            threads_joined,
            text,
            say,
            app_data.client,
        )

    @app_data.event("message")
    async def message_data(body, say):
        """
        데이터 봇에게 DM으로 질문하면 호출되는 이벤트

        답변은 질문 메시지의 스레드에 답니다.
        이어지는 질문은 같은 스레드에서 해야 앞선 대화를 문맥으로 사용합니다.
        """
        if is_duplicate_event(body):
            return

        event = body["event"]

        # DM만 처리 (채널 메시지는 멘션 핸들러가 담당)
        if event.get("channel_type") != "im":
            return

        # 봇 메시지와 편집·삭제 등 서브타입 이벤트는 무시
        if event.get("bot_id") or event.get("subtype"):
            return

        thread_ts = event.get("thread_ts") or event["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        user_real_name, threads_joined = await collect_thread_context(
            app_data.client, channel, thread_ts, user
        )

        await answer_data_analysis(
            thread_ts,
            channel,
            user_real_name,
            threads_joined,
            text,
            say,
            app_data.client,
        )


async def answer_data_analysis(
    thread_ts: str,
    channel: str,
    user_real_name: str,
    threads_joined: str,
    text: str,
    say,
    slack_client,
):
    """
    데이터 분석 질문에 답변합니다.

    Args:
        thread_ts: 스레드 타임스탬프
        channel: 채널 ID
        user_real_name: 사용자 이름
        threads_joined: 스레드 대화 내용
        text: 질문 내용
        say: 메시지 전송 함수
        slack_client: Slack 클라이언트

    Returns:
        None
    """
    today_str = datetime.now(tz=KST).strftime("%Y-%m-%d(%A)")

    messages: list[BaseMessage] = [
        SystemMessage(
            content=(
                "Context:\n"
                "- You are a data analyst integrated in Slack.\n"
                "- Your answer will be sent to the Slack thread.\n"
                "- We are an edu-tech startup in Korea. So always answer in Korean.\n"
                f"- Today's date is {today_str}\n"
                "\n" + DATA_ANALYSIS_GUIDE + "\n"
                "**슬랙 텍스트 포맷팅**:\n"
                "- 슬랙은 마크다운이 아닌 자체 mrkdwn 포맷을 사용합니다.\n"
                "- Bold: `*텍스트*` (별표 1개, **텍스트** 형식은 작동하지 않음)\n"
                "- Italic: `_텍스트_` (언더스코어)\n"
                "- Strikethrough: `~텍스트~` (물결표)\n"
                "- Code: `` `코드` `` (백틱)\n"
                "- Code block: ``` ```코드 블록``` ``` (백틱 3개)\n"
                "\n"
                "- 결과를 명확하고 간결하게 설명하고, 필요시 시각화를 권장합니다.\n"
                "- 쿼리 작성 시 Athena(Presto) SQL 문법을 사용합니다.\n"
            )
        )
    ]

    if threads_joined:
        messages.append(
            HumanMessage(
                content=(
                    f"{threads_joined}\n"
                    f"위는 슬랙에서 진행된 대화입니다. {user_real_name}이(가) 위 대화에 기반하여 질문합니다.\n"
                    f"{text}\n"
                )
            )
        )
    else:
        messages.append(HumanMessage(content=f"{user_real_name}: {text}"))

    reasoning = {
        "effort": "high",  # 데이터 분석을 위한 깊이 있는 추론
        "summary": "auto",  # 자동으로 추론 요약 생성
    }
    chat_model = ChatOpenAI(
        model=DEFAULT_MODEL,
        temperature=0,
        reasoning=reasoning,
        output_version=RESPONSES_OUTPUT_VERSION,
    )

    # 데이터 분석 전용 Tools
    # execute_athena_query tool은 Slack 메시지 전송을 위해 say, thread_ts, slack_client, channel을 주입
    execute_athena_query = get_execute_athena_query_tool(
        say=say, thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )
    # execute_python tool은 차트 이미지를 슬랙에 업로드하기 위해 slack_client와 channel을 주입
    # 이 도구에는 read_sheet 도 함께 주입된다(app/tools/python_tools.py). 데이터봇도
    # 구글 시트를 읽어 Athena 결과와 맞춰 볼 수 있다 -- 도구 설명에 사용법이 있다.
    execute_python = get_execute_python_tool(
        thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )
    tools = [
        list_redash_dashboards,
        read_redash_dashboard,
        read_redash_query,
        execute_athena_query,
        execute_python,
    ]

    agent_executor = create_react_agent(chat_model, tools, debug=True)

    # 툴 호출 상태를 슬랙에 표시하는 핸들러
    tool_status_handler = ToolStatusHandler(
        say=say, thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )

    response = await agent_executor.ainvoke(
        {"messages": messages},
        {"callbacks": [tool_status_handler], "recursion_limit": 200},
    )

    agent_answer = extract_text(response["messages"][-1].content)

    # Slack 텍스트 블록은 최대 3000자까지만 지원
    # 긴 응답은 여러 메시지로 분할하여 전송
    MAX_CHARS = 3000
    if len(agent_answer) <= MAX_CHARS:
        await say(
            {
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": agent_answer},
                    }
                ]
            },
            thread_ts=thread_ts,
        )
    else:
        # 메시지를 3000자 단위로 분할
        chunks = []
        current_chunk = ""

        for line in agent_answer.split("\n"):
            # 현재 줄을 추가했을 때 3000자를 초과하면 chunk 저장
            if len(current_chunk) + len(line) + 1 > MAX_CHARS:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = line
            else:
                if current_chunk:
                    current_chunk += "\n" + line
                else:
                    current_chunk = line

        # 마지막 chunk 저장
        if current_chunk:
            chunks.append(current_chunk)

        # 각 chunk를 순차적으로 전송
        for chunk in chunks:
            await say(
                {
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": chunk},
                        }
                    ]
                },
                thread_ts=thread_ts,
            )
