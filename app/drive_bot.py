"""
Drive Bot — Google Drive 자료를 읽고 쓰는 에이전트 봇

슬랙에서 멘션하면 Google Drive를 직접 탐색하여 답한다.
필요한 만큼 파일을 찾아 읽고, 정보가 부족하면 되묻고, 요청에 따라 문서를 만들거나 고친다.
"""

from datetime import datetime

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from .common import KST, collect_thread_context, say_in_chunks
from .event_dedup import is_duplicate_event
from .tool_status_handler import ToolStatusHandler
from .tools.drive_tools import read_drive_file, search_drive_files, write_drive_file

MODEL = "claude-opus-5"

# 다단계 탐색에 여유를 두되 폭주는 막는다
RECURSION_LIMIT = 100

# thinking 이 켜져 있으므로 응답 본문과 합쳐 넉넉히 잡는다
MAX_TOKENS = 16000


def _build_system_prompt() -> str:
    today_str = datetime.now(tz=KST).strftime("%Y-%m-%d(%A)")

    return (
        "당신은 슬랙에 연결된 Google Drive 어시스턴트입니다.\n"
        "한국의 에듀테크 스타트업에서 일하며, 항상 한국어로 답합니다.\n"
        f"오늘 날짜는 {today_str}입니다.\n"
        "\n"
        "## 일하는 방식\n"
        "- 추측해서 답하지 마세요. 답에 필요한 자료는 search_drive_files로 찾고 "
        "read_drive_file로 직접 읽어 근거를 확보한 뒤에 답합니다.\n"
        "- 한 번에 끝나지 않는 일은 여러 단계로 나누어 진행합니다. "
        "검색 결과가 기대와 다르면 조건을 바꿔 다시 검색하세요.\n"
        "- 요청이 모호하면 추측으로 진행하지 말고 사용자에게 되물으세요. "
        "어떤 폴더인지, 어떤 문서인지, 결과물을 어디에 저장할지 등이 불분명하면 "
        "질문하고 답을 기다립니다.\n"
        "- 사소한 판단(파일 이름, 문단 순서 등)은 직접 결정하고 무엇을 정했는지만 알려주세요.\n"
        "\n"
        "## 파일 수정 시 주의\n"
        "- write_drive_file에 file_id를 넘기면 기존 내용이 전부 대체됩니다. "
        "일부만 고칠 때는 반드시 read_drive_file로 전체를 읽고, "
        "수정된 전체 본문을 넘기세요.\n"
        "- 기존 파일을 덮어쓰기 전에는 어떤 파일을 어떻게 바꿀지 알리고 사용자 확인을 받으세요. "
        "새 파일 생성은 확인 없이 진행해도 됩니다.\n"
        "\n"
        "## 슬랙 텍스트 포맷팅\n"
        "- 슬랙은 마크다운이 아닌 자체 mrkdwn 포맷을 사용합니다.\n"
        "- Bold: `*텍스트*` (별표 1개, **텍스트** 형식은 작동하지 않음)\n"
        "- Italic: `_텍스트_` (언더스코어)\n"
        "- Strikethrough: `~텍스트~` (물결표)\n"
        "- Code: `` `코드` `` (백틱)\n"
        "- Code block: ``` ```코드 블록``` ``` (백틱 3개)\n"
        "- 파일 링크는 <URL|파일명> 형식으로 답변에 포함하세요.\n"
    )


def _extract_text(content) -> str:
    """
    Anthropic 응답 본문에서 텍스트를 추출한다.
    thinking이 켜져 있으면 content가 블록 리스트로 반환된다.
    """
    if isinstance(content, str):
        return content

    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(texts)


def register_drive_handlers(app_drive):
    """
    Drive 봇의 이벤트 핸들러를 등록합니다.
    """

    @app_drive.event("app_mention")
    async def app_mention_drive(body, say):
        """
        슬랙에서 Drive 봇을 멘션하면 호출되는 이벤트
        """
        if is_duplicate_event(body):
            return

        event = body.get("event")
        if event is None:
            return

        # 봇이 보낸 메시지는 무시 (자기 자신을 태그하는 무한 루프 방지)
        if event.get("bot_id"):
            return

        thread_ts = event.get("thread_ts") or event["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        threads_joined, user_real_name = await collect_thread_context(
            app_drive.client, channel, thread_ts, user
        )

        await answer_drive(
            thread_ts,
            channel,
            user_real_name,
            threads_joined,
            text,
            say,
            app_drive.client,
        )


async def answer_drive(
    thread_ts: str,
    channel: str,
    user_real_name: str,
    threads_joined: str,
    text: str,
    say,
    slack_client,
):
    """
    Google Drive 관련 요청에 답변합니다.

    Args:
        thread_ts: 스레드 타임스탬프
        channel: 채널 ID
        user_real_name: 질문자 이름
        threads_joined: 스레드 이전 대화 내용
        text: 이번 질문 내용
        say: 메시지 전송 함수
        slack_client: Slack 클라이언트
    """
    messages: list[BaseMessage] = [SystemMessage(content=_build_system_prompt())]

    if threads_joined:
        messages.append(
            HumanMessage(
                content=(
                    f"{threads_joined}\n"
                    f"위는 슬랙에서 진행된 대화입니다. "
                    f"{user_real_name}이(가) 위 대화에 기반하여 요청합니다.\n"
                    f"{text}\n"
                )
            )
        )
    else:
        messages.append(HumanMessage(content=f"{user_real_name}: {text}"))

    # Claude Opus 5는 temperature 를 받지 않으며 thinking 이 기본 활성화된다
    chat_model = ChatAnthropic(model=MODEL, max_tokens=MAX_TOKENS)

    tools = [search_drive_files, read_drive_file, write_drive_file]
    agent_executor = create_react_agent(chat_model, tools, debug=True)

    tool_status_handler = ToolStatusHandler(
        say=say, thread_ts=thread_ts, slack_client=slack_client, channel=channel
    )

    response = await agent_executor.ainvoke(
        {"messages": messages},
        {"callbacks": [tool_status_handler], "recursion_limit": RECURSION_LIMIT},
    )

    agent_answer = _extract_text(response["messages"][-1].content)
    await say_in_chunks(say, agent_answer, thread_ts)
