"""노션 페이지 정규화 테스트"""

import json
from datetime import timezone
from unittest.mock import MagicMock

from service.knowledge.notion import (
    author_of,
    build_page_row,
    page_title,
    walk_page_ids,
)

EMAILS = {
    "c7c11cca-1d73-471d-9b6e-bdef51470190": "lch@team-mono.com",
    "556a1abf-4f08-40c6-878a-75890d2a88ba": "byb@team-mono.com",
}

PAGE = {
    "id": "153104cd-477e-80eb-ae76-e1c2a32c7b35",
    "url": "https://www.notion.so/153104cd477e80ebae76e1c2a32c7b35",
    "created_time": "2026-07-01T02:03:04.000Z",
    "last_edited_time": "2026-08-01T05:06:07.000Z",
    "created_by": {"object": "user", "id": "c7c11cca-1d73-471d-9b6e-bdef51470190"},
    "last_edited_by": {"object": "user", "id": "556a1abf-4f08-40c6-878a-75890d2a88ba"},
    "parent": {"type": "page_id", "page_id": "0ef104cd-477e-80e1-8571-cfd10e92339a"},
    "properties": {
        "이름": {
            "type": "title",
            "title": [{"plain_text": "Sidekiq 큐 지연 대응"}],
        }
    },
}


def test_제목은_타입으로_찾는다():
    # 제목 프로퍼티의 이름은 데이터베이스마다 다르고 타입만 title로 고정이다.
    assert page_title(PAGE) == "Sidekiq 큐 지연 대응"


def test_제목이_비어_있으면_대체_문구를_쓴다():
    empty = {"properties": {"Name": {"type": "title", "title": []}}}
    assert page_title(empty) == "제목 없음"


def test_사람은_이메일로_적는다():
    assert author_of(PAGE["created_by"], EMAILS) == "lch@team-mono.com"


def test_매핑에_없으면_봇으로_적는다():
    assert author_of({"id": "1edc05f6-2702"}, EMAILS) == "bot:1edc05f6-2702"


def test_페이지를_item_행으로_바꾼다():
    row = build_page_row(
        data_source_id=3,
        root_id="0ef104cd-477e-80e1-8571-cfd10e92339a",
        page=PAGE,
        markdown="워커를 늘려 해소했다.",
        distill_delay_seconds=900,
        user_emails=EMAILS,
    )

    assert row["data_source_id"] == 3
    assert row["external_id"] == PAGE["id"]
    assert row["url"] == PAGE["url"]
    assert row["title"] == "Sidekiq 큐 지연 대응"
    assert row["author"] == "lch@team-mono.com"
    assert row["source_updated_at"].astimezone(timezone.utc).hour == 5
    assert row["distill_state"] == "pending"
    # 마지막 편집 + 900초
    assert (row["distill_after"] - row["source_updated_at"]).total_seconds() == 900


def test_제목을_본문_앞에_붙인다():
    # 제목에만 있는 말은 raw_text에 없으면 어휘 검색으로 영영 걸리지 않는다.
    row = build_page_row(3, "root", PAGE, "본문", 900, EMAILS)

    assert row["raw_text"].startswith("Sidekiq 큐 지연 대응\n\n")
    assert "본문" in row["raw_text"]


def test_마지막_편집자를_metadata에_남긴다():
    row = build_page_row(3, "root", PAGE, "본문", 900, EMAILS)

    metadata = json.loads(row["metadata"])
    assert metadata["last_edited_by"] == "byb@team-mono.com"
    assert metadata["root_page_id"] == "root"


def _children(mapping: dict[str, list[dict]]) -> MagicMock:
    """blocks.children.list를 흉내내는 클라이언트를 만듭니다."""
    client = MagicMock()
    client.blocks.children.list.side_effect = lambda block_id, **_: {
        "results": mapping.get(block_id, []),
        "next_cursor": None,
    }
    return client


def test_하위_페이지를_전부_훑는다():
    client = _children(
        {
            "root": [{"id": "a", "type": "child_page"}],
            "a": [{"id": "b", "type": "child_page"}],
        }
    )

    assert list(walk_page_ids(client, "root")) == ["root", "a", "b"]


def test_토글_안에_있는_하위_페이지도_찾는다():
    # 토글이나 컬럼 안에 페이지를 넣는 사람이 있다.
    client = _children(
        {
            "root": [{"id": "t", "type": "toggle", "has_children": True}],
            "t": [{"id": "a", "type": "child_page"}],
        }
    )

    assert list(walk_page_ids(client, "root")) == ["root", "a"]


def test_같은_페이지를_두_번_내지_않는다():
    # synced_block은 원본을 여러 곳에서 가리킨다.
    client = _children(
        {
            "root": [
                {"id": "a", "type": "child_page"},
                {"id": "a", "type": "child_page"},
            ],
            "a": [{"id": "root", "type": "child_page"}],
        }
    )

    assert list(walk_page_ids(client, "root")) == ["root", "a"]
