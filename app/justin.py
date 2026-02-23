"""
Justin Bot — 미팅 보고서 / 제안서 자동 피드백 봇

Slack에서 @Justin을 멘션하면서 Notion 페이지 링크를 전달하면,
Justin_Project의 MD 파일을 기반으로 피드백을 자동 생성합니다.
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from notion2md.exporter.block import StringExporter

from .common import slack_users_list
from .tool_status_handler import ToolStatusHandler

KST = ZoneInfo("Asia/Seoul")

# Justin 프롬프트 MD 파일 캐시
_prompts_cache: dict[str, str] = {}


def _load_prompts_dir() -> Path:
    """Justin_Project 디렉토리 경로를 반환합니다."""
    prompts_dir = os.environ.get("JUSTIN_PROMPTS_DIR", "")
    if prompts_dir:
        return Path(prompts_dir)
    # 기본값: tmn-workflow-automation과 같은 레벨의 Justin_Project
    return Path(__file__).resolve().parent.parent.parent / "Justin_Project"


def _load_prompt_file(filename: str) -> str:
    """MD 파일을 로드하고 캐싱합니다."""
    if filename in _prompts_cache:
        return _prompts_cache[filename]

    filepath = _load_prompts_dir() / filename
    content = filepath.read_text(encoding="utf-8")
    _prompts_cache[filename] = content
    return content


def extract_notion_page_id(text: str) -> str | None:
    """Slack 메시지에서 Notion 페이지 ID(32자 hex)를 추출합니다."""
    # Slack이 URL을 <url|label> 형식으로 감쌀 수 있으므로 < > 안도 탐색
    # notion.so/xxx-<32hex> 또는 notion.so/<32hex> 또는 notion.site/...
    match = re.search(
        r"notion\.(?:so|site)/(?:[\w-]+/)*(?:[\w-]+-)?([a-f0-9]{32})", text
    )
    if match:
        return match.group(1)

    # 하이픈이 포함된 UUID 형식 (8-4-4-4-12)
    match = re.search(
        r"notion\.(?:so|site)/(?:[\w-]+/)*(?:[\w-]+-)?([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        text,
    )
    if match:
        return match.group(1).replace("-", "")

    return None


def detect_document_type(content: str) -> Literal["meeting", "proposal"]:
    """노션 페이지 내용을 분석하여 미팅 보고서 / 제안서를 판별합니다."""
    meeting_keywords = [
        "미팅 보고",
        "미팅보고",
        "회의록",
        "미팅 일시",
        "미팅 일정",
        "미팅 참석자",
        "참석자",
        "Follow-up",
        "후속 협업 가능성",
        "즉시 전달",
    ]
    proposal_keywords = [
        "제안서",
        "제안 배경",
        "사업 개요",
        "RFP",
        "수행 계획",
        "추진 전략",
        "기대 효과",
        "사업 이해",
        "입찰",
    ]

    meeting_score = sum(1 for kw in meeting_keywords if kw in content)
    proposal_score = sum(1 for kw in proposal_keywords if kw in content)

    return "proposal" if proposal_score > meeting_score else "meeting"


def _build_system_prompt(doc_type: Literal["meeting", "proposal"]) -> str:
    """문서 유형에 따라 시스템 프롬프트를 구성합니다."""
    today_str = datetime.now(tz=KST).strftime("%Y-%m-%d(%A)")

    feedback_all = _load_prompt_file("Feedback_all.md")

    if doc_type == "meeting":
        feedback_guide = _load_prompt_file("meeting_feedback.md")
        role_desc = "미팅 보고서 피드백"
    else:
        feedback_guide = _load_prompt_file("proposal_feedback.md")
        role_desc = "제안서 피드백"

    return (
        f"당신은 팀모노리스의 AI 피드백 시스템 *Justin*입니다.\n"
        f"오늘 날짜: {today_str}\n\n"
        f"## 역할\n"
        f"직원이 제출한 {role_desc}을(를) 수행합니다.\n"
        f"아래 피드백 가이드와 팀 역할 가이드를 철저히 준수하여 피드백을 작성하세요.\n\n"
        f"## 슬랙 텍스트 포맷팅\n"
        f"- 슬랙은 마크다운이 아닌 자체 mrkdwn 포맷을 사용합니다.\n"
        f"- Bold: `*텍스트*` (별표 1개, **텍스트** 형식은 작동하지 않음)\n"
        f"- Italic: `_텍스트_` (언더스코어)\n"
        f"- Strikethrough: `~텍스트~` (물결표)\n"
        f"- Code: `` `코드` `` (백틱)\n"
        f"- Code block: ``` ```코드 블록``` ``` (백틱 3개)\n\n"
        f"---\n\n"
        f"# 피드백 가이드\n\n{feedback_guide}\n\n"
        f"---\n\n"
        f"# 팀 역할 가이드 & 직원별 프로파일\n\n{feedback_all}\n"
    )


def register_justin_handlers(app):
    """
    Justin 봇의 이벤트 핸들러를 등록합니다.
    """

    @app.event("app_mention")
    async def justin_app_mention(body, say):
        """
        Slack에서 @Justin을 멘션하면 Notion 페이지를 읽고 피드백을 생성합니다.
        """
        event = body.get("event")
        if event is None:
            return

        thread_ts = event.get("thread_ts") or event["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        # Notion 페이지 ID 추출
        page_id = extract_notion_page_id(text)
        if not page_id:
            await say(
                "Notion 페이지 링크를 함께 보내주세요.\n"
                "예: `@Justin https://www.notion.so/your-page-id`",
                thread_ts=thread_ts,
            )
            return

        # 진행 상태 표시
        status_msg = await say(
            ":hourglass_flowing_sand: Notion 페이지를 읽고 있습니다...",
            thread_ts=thread_ts,
        )

        # Notion 페이지 → 마크다운 변환
        try:
            page_content = StringExporter(block_id=page_id, output_path="test").export()
        except Exception as e:
            await say(
                f"Notion 페이지를 읽는 데 실패했습니다. 페이지 ID를 확인해주세요.\n"
                f"오류: `{e}`",
                thread_ts=thread_ts,
            )
            return

        if not page_content or not page_content.strip():
            await say(
                "Notion 페이지의 내용이 비어 있습니다. 페이지를 확인해주세요.",
                thread_ts=thread_ts,
            )
            return

        # 문서 유형 감지
        doc_type = detect_document_type(page_content)

        # 상태 업데이트
        doc_type_label = "미팅 보고서" if doc_type == "meeting" else "제안서"
        await app.client.chat_update(
            channel=channel,
            ts=status_msg["ts"],
            text=f":hourglass_flowing_sand: {doc_type_label} 피드백을 작성 중입니다...",
        )

        # 작성자 정보 조회
        user_real_name = "Unknown"
        if user:
            user_info_list = await slack_users_list(app.client)
            user_dict = {
                u["id"]: u for u in user_info_list["members"] if u["id"] == user
            }
            user_profile = user_dict.get(user, {})
            user_real_name = user_profile.get("real_name", "Unknown")

        # 시스템 프롬프트 구성
        system_prompt = _build_system_prompt(doc_type)

        # 사용자 메시지 구성
        human_message = (
            f"아래는 {user_real_name}님이 제출한 {doc_type_label}입니다.\n"
            f"피드백 가이드에 따라 피드백을 작성해주세요.\n\n"
            f"---\n\n"
            f"{page_content}"
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_message),
        ]

        # Claude로 피드백 생성
        chat_model = ChatAnthropic(model="claude-sonnet-4-20250514")

        response = await chat_model.ainvoke(messages)
        feedback = response.content

        # 상태 메시지 삭제
        await app.client.chat_delete(
            channel=channel,
            ts=status_msg["ts"],
        )

        # 피드백 전송
        await say(
            {
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": feedback},
                    }
                ]
            },
            thread_ts=thread_ts,
        )
