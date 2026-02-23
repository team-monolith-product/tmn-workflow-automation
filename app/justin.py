"""
Justin Bot — 미팅 보고서 / 제안서 자동 피드백 봇

Slack에서 @Justin을 멘션하면서 Notion 페이지 링크 또는 PDF 첨부파일을 전달하면,
Justin 프롬프트 MD 파일을 기반으로 피드백을 자동 생성합니다.

- 미팅 보고서: Notion 링크 → 마크다운 변환 → 피드백
- 제안서: PDF 첨부파일 → 페이지별 이미지 변환 → Claude Vision → 피드백
"""

import base64
import io
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import aiohttp
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from notion2md.exporter.block import StringExporter
from pdf2image import convert_from_bytes
from PIL import Image

from .common import slack_users_list
from .tool_status_handler import ToolStatusHandler

KST = ZoneInfo("Asia/Seoul")

# Justin 프롬프트 MD 파일 캐시
_prompts_cache: dict[str, str] = {}

# PDF 페이지 처리 제한
MAX_PDF_PAGES = 50


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "justin_prompts"


def _load_prompt_file(filename: str) -> str:
    """MD 파일을 로드하고 캐싱합니다."""
    if filename in _prompts_cache:
        return _prompts_cache[filename]

    filepath = PROMPTS_DIR / filename
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

    if doc_type == "meeting":
        feedback_guide = _load_prompt_file("meeting_feedback.md")
        role_desc = "미팅 보고서 피드백"
        extra_guides = ""
    else:
        feedback_guide = _load_prompt_file("proposal_feedback.md")
        proposal_review = _load_prompt_file("proposal_review_page_by_page.md")
        role_desc = "제안서 피드백"
        extra_guides = (
            f"\n\n---\n\n" f"# 제안서 페이지별 리뷰 가이드\n\n{proposal_review}\n"
        )

    return (
        f"당신은 팀모노리스의 AI 피드백 시스템 *Justin*입니다.\n"
        f"오늘 날짜: {today_str}\n\n"
        f"## 역할\n"
        f"직원이 제출한 {role_desc}을(를) 수행합니다.\n"
        f"아래 피드백 가이드를 철저히 준수하여 피드백을 작성하세요.\n\n"
        f"## 슬랙 텍스트 포맷팅\n"
        f"- 슬랙은 마크다운이 아닌 자체 mrkdwn 포맷을 사용합니다.\n"
        f"- Bold: `*텍스트*` (별표 1개, **텍스트** 형식은 작동하지 않음)\n"
        f"- Italic: `_텍스트_` (언더스코어)\n"
        f"- Strikethrough: `~텍스트~` (물결표)\n"
        f"- Code: `` `코드` `` (백틱)\n"
        f"- Code block: ``` ```코드 블록``` ``` (백틱 3개)\n\n"
        f"---\n\n"
        f"# 피드백 가이드\n\n{feedback_guide}"
        f"{extra_guides}\n"
    )


async def _download_slack_file(file_url: str, bot_token: str) -> bytes:
    """Slack 파일을 다운로드합니다."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            file_url, headers={"Authorization": f"Bearer {bot_token}"}
        ) as resp:
            resp.raise_for_status()
            return await resp.read()


def _pdf_to_base64_images(
    pdf_bytes: bytes, max_pages: int = MAX_PDF_PAGES
) -> list[str]:
    """PDF를 페이지별 base64 인코딩 JPEG 이미지로 변환합니다."""
    images = convert_from_bytes(pdf_bytes, dpi=200)
    result = []
    for i, img in enumerate(images[:max_pages]):
        # 가로 폭 1600px로 리사이즈 (API 비용 최적화)
        if img.width > 1600:
            ratio = 1600 / img.width
            img = img.resize((1600, int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        result.append(b64)
    return result


def _extract_pdf_files(event: dict) -> list[dict]:
    """Slack 이벤트에서 PDF 파일 정보를 추출합니다."""
    files = event.get("files", [])
    return [
        f
        for f in files
        if f.get("mimetype") == "application/pdf"
        or (f.get("name", "").lower().endswith(".pdf"))
    ]


def register_justin_handlers(app):
    """
    Justin 봇의 이벤트 핸들러를 등록합니다.
    """

    @app.event("app_mention")
    async def justin_app_mention(body, say):
        """
        Slack에서 @Justin을 멘션하면 Notion 페이지 또는 PDF를 읽고 피드백을 생성합니다.

        - Notion 링크가 있으면 → 미팅 보고서 피드백
        - PDF 첨부파일이 있으면 → 제안서 피드백 (Claude Vision)
        """
        event = body.get("event")
        if event is None:
            return

        thread_ts = event.get("thread_ts") or event["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        # 입력 소스 판별: PDF 첨부 vs Notion 링크
        pdf_files = _extract_pdf_files(event)
        page_id = extract_notion_page_id(text)

        if not pdf_files and not page_id:
            await say(
                "피드백할 문서를 함께 보내주세요.\n"
                "• 미팅 보고서: `@Justin <Notion 페이지 링크>`\n"
                "• 제안서: `@Justin` + PDF 파일 첨부",
                thread_ts=thread_ts,
            )
            return

        # 작성자 정보 조회
        user_real_name = "Unknown"
        if user:
            user_info_list = await slack_users_list(app.client)
            user_dict = {
                u["id"]: u for u in user_info_list["members"] if u["id"] == user
            }
            user_profile = user_dict.get(user, {})
            user_real_name = user_profile.get("real_name", "Unknown")

        # --- PDF 제안서 피드백 ---
        if pdf_files:
            await _handle_pdf_feedback(
                app, say, pdf_files, user_real_name, thread_ts, channel
            )
            return

        # --- Notion 미팅 보고서 피드백 ---
        await _handle_notion_feedback(
            app, say, page_id, user_real_name, text, thread_ts, channel
        )


async def _handle_notion_feedback(
    app, say, page_id, user_real_name, text, thread_ts, channel
):
    """Notion 페이지 기반 피드백을 처리합니다."""
    status_msg = await say(
        ":hourglass_flowing_sand: Notion 페이지를 읽고 있습니다...",
        thread_ts=thread_ts,
    )

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

    doc_type = detect_document_type(page_content)
    doc_type_label = "미팅 보고서" if doc_type == "meeting" else "제안서"

    await app.client.chat_update(
        channel=channel,
        ts=status_msg["ts"],
        text=f":hourglass_flowing_sand: {doc_type_label} 피드백을 작성 중입니다...",
    )

    system_prompt = _build_system_prompt(doc_type)
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

    chat_model = ChatAnthropic(model="claude-opus-4-20250514")
    response = await chat_model.ainvoke(messages)
    feedback = response.content

    await app.client.chat_delete(channel=channel, ts=status_msg["ts"])

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


async def _handle_pdf_feedback(app, say, pdf_files, user_real_name, thread_ts, channel):
    """PDF 첨부파일 기반 제안서 피드백을 처리합니다."""
    pdf_file = pdf_files[0]  # 첫 번째 PDF만 처리
    file_name = pdf_file.get("name", "제안서.pdf")

    status_msg = await say(
        f":hourglass_flowing_sand: `{file_name}` PDF를 분석 중입니다...",
        thread_ts=thread_ts,
    )

    # Slack에서 PDF 다운로드
    file_url = pdf_file.get("url_private_download") or pdf_file.get("url_private")
    bot_token = os.environ["SLACK_BOT_TOKEN_JUSTIN"]

    try:
        pdf_bytes = await _download_slack_file(file_url, bot_token)
    except Exception as e:
        await say(
            f"PDF 파일을 다운로드하는 데 실패했습니다.\n오류: `{e}`",
            thread_ts=thread_ts,
        )
        return

    # PDF → 페이지별 이미지 변환
    await app.client.chat_update(
        channel=channel,
        ts=status_msg["ts"],
        text=f":hourglass_flowing_sand: `{file_name}` PDF를 이미지로 변환 중입니다...",
    )

    try:
        page_images = _pdf_to_base64_images(pdf_bytes)
    except Exception as e:
        await say(
            f"PDF를 이미지로 변환하는 데 실패했습니다.\n오류: `{e}`",
            thread_ts=thread_ts,
        )
        return

    total_pages = len(page_images)

    await app.client.chat_update(
        channel=channel,
        ts=status_msg["ts"],
        text=f":hourglass_flowing_sand: `{file_name}` 제안서 피드백을 작성 중입니다... ({total_pages}페이지)",
    )

    # Claude Vision 메시지 구성
    system_prompt = _build_system_prompt("proposal")

    # HumanMessage content를 멀티모달로 구성 (텍스트 + 이미지들)
    content_parts = [
        {
            "type": "text",
            "text": (
                f"{user_real_name}님이 제출한 제안서 `{file_name}` ({total_pages}페이지)입니다.\n"
                f"각 페이지 이미지를 순서대로 확인하고, 피드백 가이드에 따라 피드백을 작성해주세요.\n"
                f"페이지별 리뷰 가이드가 있다면 해당 가이드도 참고하세요."
            ),
        }
    ]

    for i, b64_img in enumerate(page_images):
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_img}",
                },
            }
        )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=content_parts),
    ]

    chat_model = ChatAnthropic(model="claude-opus-4-20250514")
    response = await chat_model.ainvoke(messages)
    feedback = response.content

    await app.client.chat_delete(channel=channel, ts=status_msg["ts"])

    # Slack 메시지 길이 제한(3000자) 대응: 길면 여러 블록으로 분할
    if len(feedback) <= 3000:
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
    else:
        # 3000자씩 분할하여 전송
        chunks = [feedback[i : i + 3000] for i in range(0, len(feedback), 3000)]
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
