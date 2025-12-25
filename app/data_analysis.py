"""
데이터 분석 Agent
"""

from datetime import datetime
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.common import KST
from app.tools.athena_tools import execute_athena_query
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
                "- **중요: database 파라미터는 필수입니다!**\n"
                "  - execute_athena_query 도구를 사용할 때 반드시 database 파라미터를 지정해야 합니다.\n"
                "  - Redash 쿼리에서 'database.table' 패턴을 찾아 데이터베이스 이름을 파악하세요.\n"
                '  - 예: SELECT * FROM analytics.users → database="analytics"\n'
                "  - 데이터베이스를 지정하지 않으면 쿼리가 실패합니다.\n"
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

    # GPT-5.2 모델 사용 (tech.md에 명시된 대로)
    # 현재는 gpt-4.1을 사용하고, GPT-5.2가 출시되면 업데이트 예정
    chat_model = ChatOpenAI(model="gpt-4.1", temperature=0)

    # 데이터 분석 전용 Tools
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

    await say(
        {
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": agent_answer}}
            ]
        },
        thread_ts=thread_ts,
    )
