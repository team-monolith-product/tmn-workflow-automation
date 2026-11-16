"""불완전한 파일과 빈 표가 있는 페이지의 실제 변환기 회귀 테스트."""

from unittest.mock import Mock

import pytest

from service.notion_markdown import page_to_markdown

PAGE_ID = "00000000-0000-0000-0000-000000000001"
FILE_ID = "00000000-0000-0000-0000-000000000002"
BLOCK_URL = (
    f"https://www.notion.so/{PAGE_ID.replace('-', '')}#{FILE_ID.replace('-', '')}"
)


def _text(text):
    return {"type": "text", "plain_text": text, "text": {"content": text}}


def _paragraph(text):
    return {
        "id": text,
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": [_text(text)]},
    }


def _file(content):
    return {"id": FILE_ID, "type": "file", "has_children": False, "file": content}


def _client(blocks):
    client = Mock()
    client.blocks.children.list.return_value = {
        "results": blocks,
        "has_more": False,
        "next_cursor": None,
    }
    return client


def test_file_without_type_keeps_filename_and_surrounding_text():
    # Sentry의 실제 실패 응답: file 안에 caption과 name만 있었다.
    client = _client(
        [
            _paragraph("앞 본문"),
            _file({"caption": [], "name": "사업계획.pdf"}),
            _paragraph("뒤 본문"),
        ]
    )

    markdown = page_to_markdown(client, PAGE_ID)

    assert "앞 본문" in markdown
    assert f"[사업계획.pdf]({BLOCK_URL})" in markdown
    assert "뒤 본문" in markdown


def test_nested_file_keeps_caption_and_filename():
    client = _client([])
    client.blocks.children.list.side_effect = [
        {
            "results": [
                {
                    "id": "toggle",
                    "type": "toggle",
                    "has_children": True,
                    "toggle": {"rich_text": [_text("첨부자료")]},
                }
            ],
            "has_more": False,
        },
        {
            "results": [
                _file({"caption": [_text("상세 계획")], "name": "계획.pdf"}),
                _paragraph("중첩 본문"),
            ],
            "has_more": False,
        },
    ]

    markdown = page_to_markdown(client, PAGE_ID)

    assert "첨부자료" in markdown
    assert f"[상세 계획 (계획.pdf)]({BLOCK_URL})" in markdown
    assert "중첩 본문" in markdown


@pytest.mark.parametrize(
    "content,title",
    [
        ({"caption": [], "name": "계획[최종].pdf"}, r"계획\[최종\].pdf"),
        ({"caption": [_text("파일 설명")]}, "파일 설명"),
        ({"caption": [], "name": ""}, "file"),
    ],
)
def test_incomplete_file_link_labels(content, title):
    assert f"[{title}]({BLOCK_URL})" in page_to_markdown(
        _client([_file(content)]), PAGE_ID
    )


@pytest.mark.parametrize("file_type", ["file", "external"])
def test_normal_files_keep_download_links(file_type):
    url = "https://example.com/plan.pdf"
    client = _client(
        [
            _file(
                {"type": file_type, file_type: {"url": url}, "caption": [_text("계획")]}
            )
        ]
    )

    assert f"[계획]({url})" in page_to_markdown(client, PAGE_ID)


def test_api_errors_still_propagate():
    client = _client([])
    client.blocks.children.list.side_effect = TimeoutError("Notion timeout")

    with pytest.raises(TimeoutError, match="Notion timeout"):
        page_to_markdown(client, PAGE_ID)


def _table(has_children):
    return {
        "id": "table",
        "type": "table",
        "has_children": has_children,
        "table": {"table_width": 2, "has_column_header": True, "has_row_header": False},
    }


def _table_client(blocks, rows):
    client = _client([])

    def children(*, block_id, start_cursor):
        return {
            "results": rows if block_id == "table" else blocks,
            "has_more": False,
            "next_cursor": None,
        }

    client.blocks.children.list.side_effect = children
    return client


@pytest.mark.parametrize("has_children", [False, True])
def test_empty_table_preserves_surrounding_text(has_children):
    client = _table_client(
        [_paragraph("앞 본문"), _table(has_children), _paragraph("뒤 본문")], []
    )

    markdown = page_to_markdown(client, PAGE_ID)

    assert [line for line in markdown.splitlines() if line] == ["앞 본문", "뒤 본문"]


def test_nested_empty_table_preserves_sibling_content():
    client = _client([])
    client.blocks.children.list.side_effect = [
        {
            "results": [
                {
                    "id": "toggle",
                    "type": "toggle",
                    "has_children": True,
                    "toggle": {"rich_text": [_text("상세")]},
                }
            ]
        },
        {"results": [_table(False), _paragraph("중첩 본문")]},
    ]

    markdown = page_to_markdown(client, PAGE_ID)

    assert "<summary>상세</summary>" in markdown
    assert "중첩 본문" in markdown


def test_normal_table_keeps_cells_and_rich_text():
    linked = {**_text("원문"), "href": "https://example.com"}
    bold = {**_text("중요"), "annotations": {"bold": True}}
    rows = [
        {
            "id": "header",
            "type": "table_row",
            "table_row": {"cells": [[_text("제목")], [_text("링크")]]},
        },
        {"id": "row", "type": "table_row", "table_row": {"cells": [[bold], [linked]]}},
    ]
    client = _table_client([_table(True)], rows)

    markdown = page_to_markdown(client, PAGE_ID)

    assert "제목" in markdown
    assert "링크" in markdown
    assert "**중요**" in markdown
    assert "[원문](https://example.com)" in markdown
    assert markdown.count("**중요**") == 1


def test_table_api_errors_still_propagate():
    client = _client([])
    client.blocks.children.list.side_effect = [
        {"results": [_table(True)]},
        {"results": []},
        TimeoutError("Table timeout"),
    ]

    with pytest.raises(TimeoutError, match="Table timeout"):
        page_to_markdown(client, PAGE_ID)
