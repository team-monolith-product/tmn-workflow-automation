"""노션 페이지를 본떠 만드는 복사 규칙 테스트

노션 API 대신 블록 트리를 메모리에 들고 있는 가짜 클라이언트를 씁니다. 만든 블록도
같은 트리에 넣어, 만든 블록 아래로 하위 블록을 옮기는 흐름까지 확인합니다.
"""

from itertools import count

from service import notion_page_copy


def _text(content: str) -> list[dict]:
    return [
        {
            "type": "text",
            "text": {"content": content, "link": None},
            "annotations": {"bold": False},
            "plain_text": content,
            "href": None,
        }
    ]


def _block(block_id: str, kind: str, children: list | None = None, **content) -> dict:
    return {
        "id": block_id,
        "type": kind,
        "has_children": bool(children),
        kind: content,
        "_children": children or [],
    }


class FakeNotion:
    """블록 트리를 메모리에 두는 노션 클라이언트."""

    def __init__(self, page: dict, body: list[dict]):
        self.tree: dict[str, list[dict]] = {}
        self.pages = self
        self.blocks = self
        self.children = self
        self.created_page = None
        self._ids = count(1)
        self._page = page
        self._store(page["id"], body)

    def _store(self, parent_id: str, blocks: list[dict]) -> None:
        self.tree[parent_id] = blocks
        for block in blocks:
            self._store(block["id"], block.pop("_children", []))

    # pages
    def retrieve(self, page_id):
        return self._page

    def create(self, parent, properties):
        self.created_page = {"parent": parent, "properties": properties}
        return {"id": "new-page", "url": "https://notion.so/new-page"}

    # blocks.children
    def list(self, block_id, start_cursor=None):
        return {"results": self.tree.get(block_id, []), "has_more": False}

    def append(self, block_id, children):
        made = [self._made(payload) for payload in children]
        self.tree.setdefault(block_id, []).extend(made)
        return {"results": made}

    def _made(self, payload: dict) -> dict:
        kind = payload["type"]
        content = dict(payload[kind])
        nested = content.pop("children", [])
        block = {"id": f"new-{next(self._ids)}", "type": kind, kind: content}
        block["has_children"] = bool(nested)
        self.tree[block["id"]] = [self._made(child) for child in nested]
        return block


def _source_page() -> dict:
    return {
        "id": "src",
        "parent": {"type": "data_source_id", "data_source_id": "ds-1"},
        "properties": {
            "회의명": {"type": "title", "title": _text("26.11월 회의록")},
            "회의 유형": {
                "type": "select",
                "select": {"id": "x", "name": "고객팀 회의"},
            },
            "생성 일시": {"type": "created_time", "created_time": "2026-11-03"},
        },
    }


def _created(fake: FakeNotion, parent_id: str) -> list[tuple[str, str]]:
    """만든 블록을 (종류, 글자) 로 펼칩니다."""
    result = []
    for block in fake.tree[parent_id]:
        kind = block["type"]
        text = "".join(t["text"]["content"] for t in block[kind].get("rich_text", []))
        result.append((kind, text))
    return result


def test_뺄_블록은_아래까지_빼고_글자를_바꾸며_토글_아래까지_옮긴다():
    body = [
        _block(
            "week",
            "heading_2",
            [
                _block("tmpl", "numbered_list_item", rich_text=_text("현황")),
                _block(
                    "memo",
                    "bulleted_list_item",
                    [_block("memo-child", "paragraph", rich_text=_text("세부"))],
                    rich_text=_text("수빈님 퇴사"),
                ),
            ],
            rich_text=_text("1주차(11/5)"),
            is_toggleable=True,
        ),
    ]
    fake = FakeNotion(_source_page(), body)

    # When : 메모 블록을 빼고 날짜를 바꿔 본뜨면
    copied = notion_page_copy.copy_page(
        fake, "src", "26.12월 회의록", ["memo"], [("11/5", "12/3")]
    )

    # Then : 토글 제목은 날짜가 바뀌고, 그 아래 틀만 남고 메모는 아래까지 빠진다
    [(kind, title)] = _created(fake, "new-page")
    assert (kind, title) == ("heading_2", "1주차(12/3)")
    toggle_id = fake.tree["new-page"][0]["id"]
    assert fake.tree["new-page"][0]["heading_2"]["is_toggleable"] is True
    assert _created(fake, toggle_id) == [("numbered_list_item", "현황")]
    assert copied.url == "https://notion.so/new-page" and copied.skipped == []


def test_새_페이지는_같은_데이터베이스에_제목만_바꾼_쓸_수_있는_속성으로_만든다():
    fake = FakeNotion(_source_page(), [])

    # When : 본떠 만들면
    notion_page_copy.copy_page(fake, "src", "26.12월 회의록", [], [])

    # Then : 같은 데이터 소스 아래, 제목은 새 것, 선택 속성은 이름만, 읽기 전용은 뺀다
    assert fake.created_page["parent"] == {"data_source_id": "ds-1"}
    properties = fake.created_page["properties"]
    assert properties["회의명"]["title"][0]["text"]["content"] == "26.12월 회의록"
    assert properties["회의 유형"] == {"select": {"name": "고객팀 회의"}}
    assert "생성 일시" not in properties


def test_동기화_블록은_원본을_가리키고_다시_만들_수_없는_블록은_건너뛰고_알린다():
    body = [
        _block(
            "kpi",
            "synced_block",
            [_block("db", "child_database", title="KPI")],
            synced_from=None,
        ),
        _block(
            "ref",
            "synced_block",
            synced_from={"type": "block_id", "block_id": "kpi-origin"},
        ),
        _block("sub", "child_page", title="하위 페이지"),
        _block("img", "image", type="file", file={"url": "https://s3..."}, caption=[]),
    ]
    fake = FakeNotion(_source_page(), body)

    # When : 본떠 만들면
    copied = notion_page_copy.copy_page(fake, "src", "새 페이지", [], [])

    # Then : 동기화 블록은 원본(또는 원본의 원본)을 가리키는 블록이 되고,
    #        하위 페이지와 노션에 올린 이미지는 건너뛴 목록에 남는다
    made = fake.tree["new-page"]
    assert [b["synced_block"]["synced_from"]["block_id"] for b in made] == [
        "kpi",
        "kpi-origin",
    ]
    assert fake.tree[made[0]["id"]] == []
    assert copied.skipped == ["child_page: 하위 페이지", "image: (노션에 올린 파일)"]


def test_단은_첫_단계를_함께_만들고_그_아래는_만든_뒤에_옮긴다():
    body = [
        _block(
            "cols",
            "column_list",
            [
                _block(
                    "col",
                    "column",
                    [
                        _block(
                            "t",
                            "toggle",
                            [_block("inner", "paragraph", rich_text=_text("안쪽"))],
                            rich_text=_text("토글"),
                        )
                    ],
                )
            ],
        )
    ]
    fake = FakeNotion(_source_page(), body)

    # When : 본떠 만들면
    notion_page_copy.copy_page(fake, "src", "새 페이지", [], [])

    # Then : 단 → 토글 → 안쪽 문단까지 같은 모양으로 옮겨진다
    [column_list] = fake.tree["new-page"]
    [column] = fake.tree[column_list["id"]]
    [toggle] = fake.tree[column["id"]]
    assert _created(fake, toggle["id"]) == [("paragraph", "안쪽")]
