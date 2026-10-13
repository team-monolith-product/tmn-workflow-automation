"""Notion 페이지를 파일 메타데이터가 불완전해도 마크다운으로 변환합니다."""

import re
from typing import Any

from notion_client import Client
from notion_to_md import NotionToMarkdown


def _file_without_type_to_markdown(block: dict[str, Any], page_id: str) -> str | None:
    """파일 URL을 받지 못한 블록은 제목과 원본 블록 링크를 남깁니다."""
    content = block["file"]
    if "type" in content:
        # 정상 파일은 기존 변환기의 다운로드 링크 처리를 그대로 쓴다.
        return None

    # 실제 응답에 caption과 name만 있는 파일이 있다. 이 블록 하나 때문에
    # 페이지 전체를 잃지 않도록, 없는 다운로드 URL 대신 원본으로 연결한다.
    caption = "".join(item["plain_text"] for item in content.get("caption", [])).strip()
    name = content.get("name", "")
    title = caption or name or "file"
    if caption and name and caption != name:
        title = f"{caption} ({name})"
    title = re.sub(r"([\\\[\]])", r"\\\1", title.replace("\n", " "))
    url = f"https://www.notion.so/{page_id.replace('-', '')}#{block['id'].replace('-', '')}"
    return f"[{title}]({url})"


def page_to_markdown(client: Client, page_id: str) -> str:
    """페이지 본문과 파일명을 보존하며 마크다운 문자열로 변환합니다.

    Args:
        client: Notion 클라이언트
        page_id: 변환할 페이지 ID

    Returns:
        str: 페이지 본문의 마크다운
    """
    converter = NotionToMarkdown(notion_client=client)
    converter.set_custom_transformer(
        "file", lambda block: _file_without_type_to_markdown(block, page_id)
    )
    blocks = converter.page_to_markdown(page_id)
    return converter.to_markdown_string(blocks).get("parent", "")
