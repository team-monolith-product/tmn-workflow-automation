"""
OOM Alert 분석 핸들러

Slack 스레드에서 봇 멘션 시 Grafana Container Restarts alert를 분석합니다.
LangChain ReAct 에이전트를 사용하여 CloudWatch 로그와 ALB 로그를 분석하고
OOM 원인에 대한 가설을 제시합니다.
"""

from datetime import datetime
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from app.common import KST
from app.tool_status_handler import ToolStatusHandler
from app.tools.oom_tools import (
    find_incomplete_requests,
    list_log_streams,
    query_alb_access_logs,
)

SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "oom-analyzer"


def _strip_frontmatter(content: str) -> str:
    """YAML frontmatter 제거 (--- 로 둘러싸인 부분)"""
    if content.startswith("---"):
        end_index = content.find("---", 3)
        if end_index != -1:
            return content[end_index + 3 :].strip()
    return content


def _load_system_prompt() -> str:
    """SKILL.md 파일에서 시스템 프롬프트 로드"""
    skill_md = SKILL_DIR / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    return _strip_frontmatter(content)


async def analyze_oom_alert(slack_client, body, say):
    """
    OOM alert 분석 실행

    Grafana alert 스레드에서 봇 멘션 시 호출됩니다.
    스레드의 원본 메시지(Grafana alert)를 파싱하여 분석을 시작하고,
    결과를 같은 스레드에 댓글로 게시합니다.

    Args:
        slack_client: Slack AsyncWebClient
        body: Slack 이벤트 body
        say: Slack say 함수
    """
    event = body.get("event", {})
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    user_text = event.get("text", "")

    # 스레드의 원본 메시지 가져오기
    try:
        result = await slack_client.conversations_replies(
            channel=channel, ts=thread_ts, limit=1
        )
        messages = result.get("messages", [])
        if not messages:
            await say("스레드의 원본 메시지를 찾을 수 없습니다.", thread_ts=thread_ts)
            return

        original_message = messages[0]
        alert_text = original_message.get("text", "")

        # 첨부파일에서도 텍스트 추출 (Grafana는 attachments 사용)
        attachments = original_message.get("attachments", [])
        for attachment in attachments:
            alert_text += "\n" + attachment.get("text", "")
            alert_text += "\n" + attachment.get("fallback", "")

    except Exception as e:
        await say(f"스레드 메시지 조회 중 오류: {str(e)}", thread_ts=thread_ts)
        return

    # 분석 시작 알림
    await say(
        ":mag: OOM 분석을 시작합니다...",
        thread_ts=thread_ts,
    )

    # LangChain 에이전트 설정
    today_str = datetime.now(tz=KST).strftime("%Y-%m-%d(%A)")

    system_prompt = _load_system_prompt()
    messages: list[BaseMessage] = [
        SystemMessage(content=f"{system_prompt}\n\n오늘 날짜: {today_str}"),
        HumanMessage(
            content=(
                f"다음 Grafana Container Restarts alert를 분석해주세요:\n\n"
                f"```\n{alert_text}\n```\n\n"
                f"사용자 요청: {user_text}"
            )
        ),
    ]

    chat_model = ChatAnthropic(
        model="claude-opus-4-5",
        temperature=0,
    )

    # OOM 분석 전용 도구들
    tools = [
        list_log_streams,
        find_incomplete_requests,
        query_alb_access_logs,
    ]

    agent_executor = create_react_agent(chat_model, tools, debug=True)

    # 도구 호출 상태를 슬랙에 표시하는 핸들러
    tool_status_handler = ToolStatusHandler(
        say=say, thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )

    try:
        response = await agent_executor.ainvoke(
            {"messages": messages},
            {"callbacks": [tool_status_handler], "recursion_limit": 50},
        )

        # 최종 응답 추출
        agent_answer = response["messages"][-1].content
        if isinstance(agent_answer, list):
            # 리스트 형태인 경우 text 타입 추출
            text_items = [item for item in agent_answer if item.get("type") == "text"]
            agent_answer = text_items[0]["text"] if text_items else ""

        # Slack 메시지 전송 (3000자 제한)
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
            # 긴 메시지 분할
            chunks = []
            current_chunk = ""

            for line in agent_answer.split("\n"):
                if len(current_chunk) + len(line) + 1 > MAX_CHARS:
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk = (
                        current_chunk + "\n" + line if current_chunk else line
                    )

            if current_chunk:
                chunks.append(current_chunk)

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

    except Exception as e:
        await say(
            f":x: OOM 분석 중 오류가 발생했습니다: {str(e)}",
            thread_ts=thread_ts,
        )
