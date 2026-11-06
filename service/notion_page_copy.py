"""
노션 페이지를 찾고, 기존 페이지를 본떠 새 페이지를 만듭니다.

"이번 달 회의록이 없으면 지난달 회의록을 본떠 만들어줘" 를 위한 것입니다. 마크다운을
거치면 토글 제목, 동기화 블록, 인라인 데이터베이스가 사라져서 블록을 그대로 옮깁니다.

    - 동기화 블록은 원본을 가리키는 동기화 블록으로 만듭니다. KPI 처럼 여러 페이지가
      함께 보는 내용이 끊기지 않습니다.
    - 하위 페이지, 데이터베이스, 노션에 올린 파일처럼 API 로 다시 만들 수 없는 블록은
      건너뛰고 무엇을 건너뛰었는지 돌려줍니다.
    - 지난 내용은 빼고 틀만 남기도록, 뺄 블록(omit)과 바꿀 글자(replacements)를
      받습니다. 무엇을 뺄지는 outline 을 본 에이전트가 고릅니다.

notion-client 는 동기라 이벤트 루프에서는 asyncio.to_thread 로 부릅니다. 호출 한도는
클라이언트의 전송 계층이 지킵니다.
"""

from dataclasses import dataclass, field
from typing import Any, Iterator

from notion_client import Client
from notion_client.helpers import iterate_paginated_api

# 한 번의 children.append 에 실을 수 있는 블록 수
APPEND_LIMIT = 100

# 만들 때 값을 그대로 다시 넣을 수 있는 속성 종류. 나머지(생성 일시, 수식, 롤업 등)는
# 노션이 채우거나 읽기 전용이다.
_WRITABLE_PROPERTIES = {
    "rich_text",
    "number",
    "select",
    "multi_select",
    "status",
    "date",
    "people",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "relation",
}

# 내용을 그대로 다시 만들 수 있는 블록. 하위 블록은 따로 옮긴다.
_PLAIN_BLOCKS = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
    "divider",
    "equation",
    "bookmark",
    "embed",
    "link_to_page",
    "breadcrumb",
    "table_of_contents",
}

# 외부 링크일 때만 다시 만들 수 있는 파일 블록. 노션에 올린 파일 주소는 한 시간 뒤 만료된다.
_FILE_BLOCKS = {"image", "video", "file", "pdf", "audio"}

# 옮기는 블록 전부. 동기화 블록은 원본을 가리키게 만들고, 표와 단은 만들 때 하위를
# 함께 보낸다. 나머지(하위 페이지, 데이터베이스 등)는 건너뛰고 알린다.
_COPYABLE = _PLAIN_BLOCKS | _FILE_BLOCKS | {"synced_block", "table", "column_list"}


@dataclass
class _Copy:
    """복사 한 번의 설정과 결과."""

    client: Client
    omit: set[str]
    replacements: list[tuple[str, str]]
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Planned:
    """만들 블록 하나와, 만든 뒤 그 아래로 할 일.

    대부분의 블록은 만든 뒤 원본의 하위(children_from)를 옮깁니다. 단·칸은 만들 때
    하위를 함께 보내야 해서, 함께 보낸 하위(inline)를 만든 블록과 짝지어 그 아래로
    다시 내려갑니다. 동기화 블록과 표는 둘 다 없습니다(원본과 이어지거나, 행을 함께
    보내고 행 아래는 없음).
    """

    payload: dict[str, Any]
    # 만든 뒤 하위를 옮겨 올 원본 블록 ID
    children_from: str | None = None
    # 만들 때 함께 보낸 하위. payload 의 children 과 같은 순서다.
    inline: list["_Planned"] = field(default_factory=list)


@dataclass(frozen=True)
class CopiedPage:
    """만든 페이지와 옮기지 못한 블록."""

    url: str
    skipped: list[str]


def _normalize(block_id: str) -> str:
    return block_id.replace("-", "")


def _plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join(item["plain_text"] for item in rich_text)


def _children(client: Client, block_id: str) -> Iterator[dict[str, Any]]:
    """블록의 바로 아래 블록을 순서대로 돌려줍니다."""
    return iterate_paginated_api(client.blocks.children.list, block_id=block_id)


def _replace(text: str, replacements: list[tuple[str, str]]) -> str:
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _rich_text(
    items: list[dict[str, Any]], replacements: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """읽은 rich_text 를 만들 때의 모양으로 바꿉니다. 글자 바꾸기는 여기서 합니다."""
    result = []
    for item in items:
        annotations = item["annotations"]
        if item["type"] == "text":
            text = {
                "content": _replace(item["text"]["content"], replacements),
                "link": item["text"]["link"],
            }
            result.append({"type": "text", "text": text, "annotations": annotations})
        elif item["type"] == "equation":
            result.append(
                {
                    "type": "equation",
                    "equation": item["equation"],
                    "annotations": annotations,
                }
            )
        elif item["mention"]["type"] in ("user", "page", "database"):
            kind = item["mention"]["type"]
            mention = {"type": kind, kind: {"id": item["mention"][kind]["id"]}}
            result.append(
                {"type": "mention", "mention": mention, "annotations": annotations}
            )
        elif item["mention"]["type"] == "date":
            mention = {"type": "date", "date": item["mention"]["date"]}
            result.append(
                {"type": "mention", "mention": mention, "annotations": annotations}
            )
        else:
            # 링크 미리보기 같은 멘션은 만들 수 없어 보이는 글자와 링크로 남긴다.
            text = {
                "content": _replace(item["plain_text"], replacements),
                "link": {"url": item["href"]} if item["href"] else None,
            }
            result.append({"type": "text", "text": text, "annotations": annotations})
    return result


def _content(block: dict[str, Any], copy: _Copy) -> dict[str, Any]:
    """블록 내용을 만들 때의 모양으로 바꿉니다. 하위 블록은 넣지 않습니다."""
    kind = block["type"]
    content = {key: value for key, value in block[kind].items() if key != "children"}
    for key in ("rich_text", "caption"):
        if key in content:
            content[key] = _rich_text(content[key], copy.replacements)
    icon = content.get("icon") or {}
    if kind == "callout" and icon.get("type") not in (None, "emoji", "external"):
        # 노션에 올린 아이콘은 다시 넣을 수 없다.
        del content["icon"]
    if kind == "table_row":
        content["cells"] = [
            _rich_text(cell, copy.replacements) for cell in content["cells"]
        ]
    return content


def _describe(block: dict[str, Any]) -> str:
    kind = block["type"]
    text = _plain_text(block[kind].get("rich_text", [])) or block[kind].get("title", "")
    return f"{kind}: {text[:80]}".rstrip()


def _kept(copy: _Copy, parent_id: str) -> list[dict[str, Any]]:
    """옮길 하위 블록. 뺄 블록과 다시 만들 수 없는 블록은 여기서 거릅니다."""
    kept = []
    for block in _children(copy.client, parent_id):
        if _normalize(block["id"]) in copy.omit:
            continue
        kind = block["type"]
        if kind in _FILE_BLOCKS and block[kind]["type"] != "external":
            copy.skipped.append(f"{_describe(block)} (노션에 올린 파일)")
        elif kind in _COPYABLE:
            kept.append(block)
        else:
            copy.skipped.append(_describe(block))
    return kept


def _plan(block: dict[str, Any], copy: _Copy) -> _Planned:
    """블록을 만들 모양으로 바꿉니다. 표·단·칸은 만들 때 하위를 함께 담습니다."""
    kind = block["type"]
    if kind == "synced_block":
        # 원본이면 그 블록을, 이미 사본이면 그 원본을 가리킨다.
        source = block["synced_block"]["synced_from"]
        origin = source["block_id"] if source else block["id"]
        return _Planned({"type": kind, kind: {"synced_from": {"block_id": origin}}})
    content = _content(block, copy)
    payload = {"type": kind, kind: content}
    if kind == "table":
        content["children"] = [
            {"type": "table_row", "table_row": _content(row, copy)}
            for row in _children(copy.client, block["id"])
        ]
        return _Planned(payload)
    if kind in ("column_list", "column"):
        children = (
            _children(copy.client, block["id"])
            if kind == "column_list"
            else _kept(copy, block["id"])
        )
        inline = [_plan(child, copy) for child in children]
        content["children"] = [child.payload for child in inline]
        return _Planned(payload, inline=inline)
    return _Planned(
        payload, children_from=block["id"] if block["has_children"] else None
    )


def _copy_descendants(planned: _Planned, created: dict[str, Any], copy: _Copy) -> None:
    """만든 블록 아래로 원본 블록의 하위 블록을 옮깁니다."""
    if planned.children_from is not None:
        _copy_children(planned.children_from, created["id"], copy)
    if planned.inline:
        made = _children(copy.client, created["id"])
        for child, created_child in zip(planned.inline, made, strict=True):
            _copy_descendants(child, created_child, copy)


def _copy_children(source_id: str, target_id: str, copy: _Copy) -> None:
    planned = [_plan(block, copy) for block in _kept(copy, source_id)]
    for start in range(0, len(planned), APPEND_LIMIT):
        chunk = planned[start : start + APPEND_LIMIT]
        created = copy.client.blocks.children.append(
            block_id=target_id, children=[p.payload for p in chunk]
        )["results"]
        for item, made in zip(chunk, created, strict=True):
            _copy_descendants(item, made, copy)


def _properties(page: dict[str, Any], title: str) -> dict[str, Any]:
    """원본 페이지의 속성을 만들 때의 모양으로 옮기고 제목만 바꿉니다."""
    result = {}
    for name, prop in page["properties"].items():
        kind = prop["type"]
        value = prop[kind]
        if kind == "title":
            result[name] = {"title": [{"type": "text", "text": {"content": title}}]}
        elif kind not in _WRITABLE_PROPERTIES:
            continue
        elif kind == "rich_text":
            result[name] = {"rich_text": _rich_text(value, [])}
        elif kind in ("select", "status"):
            if value is not None:
                result[name] = {kind: {"name": value["name"]}}
        elif kind == "multi_select":
            result[name] = {kind: [{"name": option["name"]} for option in value]}
        elif kind in ("people", "relation"):
            result[name] = {kind: [{"id": item["id"]} for item in value]}
        else:
            result[name] = {kind: value}
    return result


def _parent(page: dict[str, Any]) -> dict[str, str]:
    parent = page["parent"]
    kind = parent["type"]
    if kind not in ("data_source_id", "page_id"):
        raise ValueError(f"{kind} 아래 페이지는 본떠 만들 수 없습니다.")
    return {kind: parent[kind]}


def search_pages(client: Client, query: str) -> list[dict[str, str]]:
    """제목으로 페이지를 찾습니다. 최근에 고친 페이지부터 돌려줍니다.

    Args:
        client: 노션 클라이언트
        query: 제목에 들어간 글자

    Returns:
        list[dict[str, str]]: 제목·주소·ID·만든 시각·상위(데이터 소스나 페이지) ID
    """
    found = client.search(
        query=query,
        filter={"property": "object", "value": "page"},
        sort={"direction": "descending", "timestamp": "last_edited_time"},
        page_size=20,
    )
    pages = []
    for page in found["results"]:
        title = next(
            _plain_text(prop["title"])
            for prop in page["properties"].values()
            if prop["type"] == "title"
        )
        parent = page["parent"]
        pages.append(
            {
                "title": title,
                "url": page["url"],
                "id": page["id"],
                "created_time": page["created_time"],
                "parent": parent.get(parent["type"], parent["type"]),
            }
        )
    return pages


def outline(client: Client, page_id: str) -> str:
    """페이지의 블록 구조를 ID 와 함께 들여쓴 줄로 돌려줍니다.

    본떠 만들 때 뺄 블록을 고르는 데 씁니다. 동기화 블록은 통째로 연결되므로
    그 안은 펼치지 않습니다.

    Args:
        client: 노션 클라이언트
        page_id: 페이지 ID

    Returns:
        str: "  [블록ID] 종류: 글자" 줄 목록
    """
    lines = []

    def walk(block_id: str, depth: int) -> None:
        for block in _children(client, block_id):
            kind = block["type"]
            lines.append(f"{'  ' * depth}[{block['id']}] {_describe(block)}")
            if block["has_children"] and kind not in (
                "synced_block",
                "child_page",
                "child_database",
            ):
                walk(block["id"], depth + 1)

    walk(page_id, 0)
    return "\n".join(lines)


def copy_page(
    client: Client,
    source_page_id: str,
    title: str,
    omit_block_ids: list[str],
    replacements: list[tuple[str, str]],
) -> CopiedPage:
    """원본과 같은 곳(같은 데이터베이스나 상위 페이지)에 원본을 본뜬 페이지를 만듭니다.

    속성은 제목만 바꿔 그대로 옮깁니다. 본문은 뺄 블록(그 아래 전부 포함)을 빼고
    블록 그대로 옮기며, 글자는 replacements 순서대로 바꿉니다.

    Args:
        client: 노션 클라이언트
        source_page_id: 본뜰 페이지 ID
        title: 새 페이지 제목
        omit_block_ids: 옮기지 않을 블록 ID
        replacements: (바꿀 글자, 새 글자) 목록. 예: ("11월", "12월")

    Returns:
        CopiedPage: 새 페이지 주소와 옮기지 못한 블록
    """
    source = client.pages.retrieve(page_id=source_page_id)
    created = client.pages.create(
        parent=_parent(source), properties=_properties(source, title)
    )
    copy = _Copy(client, {_normalize(i) for i in omit_block_ids}, replacements)
    _copy_children(source["id"], created["id"], copy)
    return CopiedPage(created["url"], copy.skipped)
