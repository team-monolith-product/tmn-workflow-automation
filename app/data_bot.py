"""
데이터 봇 전용 로직
"""

from datetime import datetime
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .common import KST, slack_users_list
from .tool_status_handler import ToolStatusHandler
from .tools.athena_tools import get_execute_athena_query_tool
from .tools.chart_tools import get_execute_python_with_chart_tool
from .tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)


def register_data_handlers(app_data):
    """
    데이터 봇의 이벤트 핸들러를 등록합니다.
    """

    @app_data.event("app_mention")
    async def app_mention_data(body, say):
        """
        슬랙에서 데이터 봇을 멘션하여 데이터 분석을 시작하면 호출되는 이벤트
        """
        event = body.get("event")

        if event is None:
            return

        thread_ts = event.get("thread_ts") or body["event"]["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        # 스레드의 모든 메시지를 가져옴
        result = await app_data.client.conversations_replies(
            channel=channel, ts=thread_ts
        )

        # 메시지에서 사용자 ID를 수집
        user_ids = set(
            message["user"] for message in result["messages"] if "user" in message
        )
        if user:
            user_ids.add(user)

        # 사용자 정보 일괄 조회
        user_info_list = await slack_users_list(app_data.client)
        user_dict = {
            user["id"]: user
            for user in user_info_list["members"]
            if user["id"] in user_ids
        }

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
                "\n"
                "Instructions:\n"
                "- 데이터 분석 요청에 대해 먼저 Redash 대시보드를 검색하여 관련 쿼리를 찾습니다.\n"
                "- Redash에서 찾은 쿼리를 참고하여 Athena에서 SQL 쿼리를 실행합니다.\n"
                "\n"
                "**중요**: 사용자에게 쿼리 결과를 보여주고 싶을 때:\n"
                "- execute_athena_query 도구를 호출할 때 반드시 show_result_to_user=True를 설정해야 합니다.\n"
                "- 이 값이 true일 때만 사용자가 Slack에서 결과를 볼 수 있습니다.\n"
                "- 절대로 직접 표를 작성하여 답변하지 마세요. 반드시 show_result_to_user=True를 사용하세요.\n"
                "\n"
                "**차트 시각화**:\n"
                "- 데이터를 차트로 시각화하고 싶을 때는 execute_python_with_chart 도구를 사용하세요.\n"
                "- 이 도구는 파이썬 코드를 실행하고 matplotlib 차트를 슬랙에 자동으로 업로드합니다.\n"
                "- 코드 내에서 `execute_athena_query(query, database)` 함수를 직접 호출할 수 있습니다.\n"
                "- plt.savefig()나 plt.show()를 호출하지 마세요. 자동으로 처리됩니다.\n"
                "\n"
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

    # GPT-5.2 모델 사용 - OpenAI의 최신 플래그십 모델 (2025년 12월 출시)
    # 데이터 분석, 긴 컨텍스트 이해, 도구 호출에 최적화됨
    # reasoning 파라미터로 확장된 추론 능력 활성화
    reasoning = {
        "effort": "high",  # 데이터 분석을 위한 깊이 있는 추론
        "summary": "auto",  # 자동으로 추론 요약 생성
    }
    chat_model = ChatOpenAI(
        model="gpt-5.2",
        temperature=0,
        reasoning=reasoning,
        output_version="responses/v1",
    )

    # 데이터 분석 전용 Tools
    # execute_athena_query tool은 Slack 메시지 전송을 위해 say, thread_ts, slack_client, channel을 주입
    execute_athena_query = get_execute_athena_query_tool(
        say=say, thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )
    # execute_python_with_chart tool은 차트 이미지를 슬랙에 업로드하기 위해 slack_client와 channel을 주입
    execute_python_with_chart = get_execute_python_with_chart_tool(
        say=say, thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )
    tools = [
        list_redash_dashboards,
        read_redash_dashboard,
        read_redash_query,
        execute_athena_query,
        execute_python_with_chart,
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

    # GPT-5.2 reasoning 모드에서는 content가 리스트로 반환될 수 있음
    # [{'type': 'reasoning', ...}, {'type': 'text', 'text': '실제 응답', ...}]
    content = response["messages"][-1].content
    if isinstance(content, list):
        # reasoning 모드: type='text'인 항목에서 text 추출
        text_items = [item for item in content if item.get("type") == "text"]
        agent_answer = text_items[0]["text"] if text_items else ""
    else:
        # 일반 모드: 문자열 그대로 사용
        agent_answer = content

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
