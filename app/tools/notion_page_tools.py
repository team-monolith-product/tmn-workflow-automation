"""
노션 페이지를 찾고, 기존 페이지를 본떠 새 페이지를 만드는 도구입니다.

"이번 달 회의록이 있으면 그걸 쓰고, 없으면 지난달 회의록을 본떠 만들어줘" 를
에이전트가 이렇게 풉니다.

    ① search_notion_pages 로 이번 달 페이지를 찾는다. 있으면 그걸 쓴다
    ② 없으면 지난달 페이지를 찾아 get_notion_page_outline 으로 블록 구조를 본다
    ③ duplicate_notion_page 로 지난 내용은 빼고(omit) 날짜·제목은 바꿔서(replacements)
       같은 데이터베이스에 새 페이지를 만든다

복사 규칙은 service/notion_page_copy.py 에 있습니다.
"""

import asyncio

from langchain_core.tools import StructuredTool, tool
from notion_client import APIResponseError
from pydantic import BaseModel, Field

from service import notion_page_copy

from ..common import NotionPageId, notion


class TextReplacement(BaseModel):
    """새 페이지에서 바꿀 글자 한 쌍."""

    old: str = Field(description="원본에 있는 글자. 예: '11월'")
    new: str = Field(description="바꿀 글자. 예: '12월'")


def get_notion_page_copy_tools() -> list[StructuredTool]:
    """노션 페이지 찾기·구조 보기·본떠 만들기 도구를 돌려줍니다.

    Returns:
        list[StructuredTool]: [찾기, 구조 보기, 본떠 만들기]
    """

    @tool
    async def search_notion_pages(query: str) -> str:
        """
        제목으로 노션 페이지를 찾습니다. 최근에 고친 페이지부터 최대 20개를
        제목·링크·ID·만든 시각·상위(데이터베이스나 페이지) ID 와 함께 보여줍니다.
        연동 앱에 공유된 페이지만 보입니다.

        Args:
            query: 제목에 들어간 글자. 예: '26.11월 회의록', '회의록'

        Returns:
            찾은 페이지 목록
        """
        pages = await asyncio.to_thread(notion_page_copy.search_pages, notion, query)
        if not pages:
            return f"'{query}' 가 제목에 들어간 페이지가 없습니다."
        return "\n".join(
            f"- {p['title']} | {p['url']} | id={p['id']}"
            f" | 만든 시각 {p['created_time']} | 상위 {p['parent']}"
            for p in pages
        )

    @tool
    async def get_notion_page_outline(page_id: NotionPageId) -> str:
        """
        노션 페이지의 블록 구조를 블록 ID 와 함께 들여쓴 줄로 보여줍니다.
        duplicate_notion_page 로 본떠 만들 때 뺄 블록을 고르는 데 씁니다.
        내용을 읽기만 할 때는 get_notion_page 가 낫습니다.

        Returns:
            "  [블록ID] 종류: 글자" 줄 목록
        """
        try:
            return await asyncio.to_thread(notion_page_copy.outline, notion, page_id)
        except APIResponseError as error:
            return f"페이지 구조를 읽지 못했습니다: {error}"

    @tool
    async def duplicate_notion_page(
        source_page_id: NotionPageId,
        title: str,
        omit_block_ids: list[str] | None = None,
        replacements: list[TextReplacement] | None = None,
    ) -> str:
        """
        기존 노션 페이지를 본떠 같은 데이터베이스(또는 같은 상위 페이지)에 새 페이지를
        만듭니다. 속성은 제목만 바꿔 그대로 옮기고, 본문은 토글·표·동기화 블록까지
        블록 그대로 옮깁니다. 동기화 블록은 원본과 계속 동기화됩니다.

        지난 내용은 빼고 틀만 남기려면 먼저 get_notion_page_outline 으로 구조를 보고,
        옮기지 않을 블록 ID 를 omit_block_ids 에 줍니다. 그 블록 아래도 전부 빠집니다.
        예: 지난달 회의록의 주차별 토글에서 적어 둔 메모 블록은 빼고 항목 틀만 남긴다.
        날짜·월처럼 바꿔야 할 글자는 replacements 에 줍니다. 순서대로 모두 바꿉니다.

        같은 페이지를 두 번 만들지 않도록, 부르기 전에 search_notion_pages 로 이미
        있는지 확인합니다.

        Args:
            source_page_id: 본뜰 페이지 ID
            title: 새 페이지 제목
            omit_block_ids: 옮기지 않을 블록 ID 목록
            replacements: 바꿀 글자 목록

        Returns:
            새 페이지 링크와 옮기지 못한 블록
        """
        try:
            copied = await asyncio.to_thread(
                notion_page_copy.copy_page,
                notion,
                source_page_id,
                title,
                omit_block_ids or [],
                [(r.old, r.new) for r in replacements or []],
            )
        except APIResponseError as error:
            return f"페이지를 만들지 못했습니다: {error}"
        result = f"새 페이지를 만들었습니다: {copied.url}"
        if copied.skipped:
            result += "\n옮기지 못한 블록(API 로 다시 만들 수 없음):\n" + "\n".join(
                f"- {item}" for item in copied.skipped
            )
        return result

    return [search_notion_pages, get_notion_page_outline, duplicate_notion_page]
