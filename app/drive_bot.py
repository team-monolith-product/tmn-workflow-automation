"""
Drive Bot — Google Drive 자료를 읽고 쓰며 노션 운영 업무를 만드는 에이전트 봇

슬랙에서 멘션하면 Google Drive를 직접 탐색하여 답한다.
필요한 만큼 파일을 찾아 읽고, 정보가 부족하면 되묻고, 요청에 따라 문서를 만들거나 고치고,
후속 업무를 노션 운영 DB에 등록한다.
"""

import asyncio
from datetime import datetime

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .common import KST, collect_thread_context, say_in_chunks
from .event_dedup import is_duplicate_event
from .tool_status_handler import ToolStatusHandler
from .tools.drive_tools import read_drive_file, search_drive_files, write_drive_file
from .tools.notion_tools import get_create_ops_task_tool
from .tools.workspace_tools import read_sheet_range

MODEL = "gpt-5.4"

# 노션 운영 DB
OPS_DATABASE_ID = "3ab1cc82-0da6-8001-bf7f-c21c17e01dc2"

SLACK_WORKSPACE = "monolith-keb2010"

# 다단계 탐색에 여유를 두되 폭주는 막는다
RECURSION_LIMIT = 100


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
        "## 스프레드시트 읽기\n"
        "- 스프레드시트는 read_sheet_range로 필요한 범위만 읽습니다. "
        "read_drive_file로 통째로 읽는 것은 작은 시트에만 쓰세요.\n"
        "- 어떤 탭이 있는지 모르면 read_sheet_range를 범위 없이 먼저 호출해 구조를 파악하세요.\n"
        "- 시트 셀을 고치는 기능은 없습니다. 수정 요청을 받으면 할 수 없다고 알리세요. "
        "write_drive_file로 스프레드시트를 덮어쓰면 시트 전체가 망가지므로 절대 시도하지 마세요.\n"
        "\n"
        "## 노션 운영 업무 등록\n"
        "- 발송, 시스템, 문서, 보고, 예약·구매, 섭외, 모집, CS 같은 운영 업무를 "
        "등록해 달라고 하면 create_ops_task를 씁니다.\n"
        "- 담당자는 사람이 아니라 조직 단위입니다. 대화에서 어느 조직이 맡을지 "
        "분명하지 않으면 도구를 호출하지 말고 먼저 물어보세요.\n"
        "- 자료를 읽고 할 일을 도출하는 요청이라면, 먼저 Drive에서 근거를 확인한 뒤 "
        "무엇을 등록할지 정리해 보여주고 등록합니다.\n"
        "\n"
        "## 슬랙 텍스트 포맷팅\n"
        "- 슬랙은 마크다운이 아닌 자체 mrkdwn 포맷을 사용합니다.\n"
        "- Bold: `*텍스트*` (별표 1개, **텍스트** 형식은 작동하지 않음)\n"
        "- Italic: `_텍스트_` (언더스코어)\n"
        "- Strikethrough: `~텍스트~` (물결표)\n"
        "- Code: `` `코드` `` (백틱)\n"
        "- Code block: ``` ```코드 블록``` ``` (백틱 3개)\n"
        "- 파일과 노션 페이지 링크는 <URL|이름> 형식으로 답변에 포함하세요.\n"
    )


def _extract_text(content) -> str:
    """
    응답 본문에서 텍스트를 추출한다.
    reasoning 모드에서는 content가 블록 리스트로 반환된다.
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

        # Slack 스레드 링크 만들기 (노션 페이지에 북마크로 첨부된다)
        slack_thread_url = (
            f"https://{SLACK_WORKSPACE}.slack.com"
            f"/archives/{channel}/p{thread_ts.replace('.', '')}"
        )

        threads_joined, user_real_name = await collect_thread_context(
            app_drive.client, channel, thread_ts, user
        )

        await answer_drive(
            thread_ts,
            channel,
            user_real_name,
            threads_joined,
            text,
            slack_thread_url,
            say,
            app_drive.client,
        )


async def answer_drive(
    thread_ts: str,
    channel: str,
    user_real_name: str,
    threads_joined: str,
    text: str,
    slack_thread_url: str,
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
        slack_thread_url: 노션 페이지에 첨부할 슬랙 스레드 URL
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

    reasoning = {
        "effort": "high",
        "summary": "auto",
    }
    chat_model = ChatOpenAI(
        model=MODEL,
        temperature=0,
        reasoning=reasoning,
        output_version="responses/v1",
    )

    # 도구 생성 시 노션 스키마를 동기 조회하므로 이벤트 루프를 막지 않도록 분리한다
    # (캐시 미스일 때만 실제 호출이 나간다)
    create_ops_task = await asyncio.to_thread(
        get_create_ops_task_tool, OPS_DATABASE_ID, slack_thread_url
    )

    tools = [
        search_drive_files,
        read_drive_file,
        write_drive_file,
        read_sheet_range,
        create_ops_task,
    ]

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
