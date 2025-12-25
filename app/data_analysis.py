"""
데이터 분석 Agent
"""

from datetime import datetime
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.common import KST
from app.tools.athena_tools import get_execute_athena_query_tool
from app.tools.redash_tools import list_redash_dashboards, read_redash_dashboard


async def answer_data_analysis(
    thread_ts: str,
    channel: str,
    user_real_name: str,
    threads_joined: str,
    text: str,
    say,
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
                "- 결과를 명확하고 간결하게 설명하고, 필요시 시각화를 권장합니다.\n"
                "- 쿼리 작성 시 표준 SQL 문법을 사용합니다.\n"
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
    chat_model = ChatOpenAI(model="gpt-5.2", temperature=0)

    # 데이터 분석 전용 Tools
    # execute_athena_query tool은 Slack 메시지 전송을 위해 say와 thread_ts를 주입
    execute_athena_query = get_execute_athena_query_tool(say=say, thread_ts=thread_ts)
    tools = [list_redash_dashboards, read_redash_dashboard, execute_athena_query]

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
