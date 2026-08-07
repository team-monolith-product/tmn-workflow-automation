"""노션 페이지 정규화 테스트"""

import json
from datetime import timezone

from service.knowledge.notion import (
    author_of,
    build_page_row,
    derive_roots,
    is_database_row,
    node_title,
    parent_ref,
)

EMAILS = {
    "c7c11cca-1d73-471d-9b6e-bdef51470190": "lch@team-mono.com",
    "556a1abf-4f08-40c6-878a-75890d2a88ba": "byb@team-mono.com",
}

PAGE = {
    "object": "page",
    "id": "153104cd-477e-80eb-ae76-e1c2a32c7b35",
    "url": "https://www.notion.so/153104cd477e80ebae76e1c2a32c7b35",
    "created_time": "2026-07-01T02:03:04.000Z",
    "last_edited_time": "2026-08-01T05:06:07.000Z",
    "created_by": {"object": "user", "id": "c7c11cca-1d73-471d-9b6e-bdef51470190"},
    "last_edited_by": {"object": "user", "id": "556a1abf-4f08-40c6-878a-75890d2a88ba"},
    "parent": {"type": "page_id", "page_id": "0ef104cd-477e-80e1-8571-cfd10e92339a"},
    "properties": {
        "이름": {"type": "title", "title": [{"plain_text": "Sidekiq 큐 지연 대응"}]}
    },
}


def _page(page_id: str, parent: dict, title: str = "제목") -> dict:
    return {
        "object": "page",
        "id": page_id,
        "parent": parent,
        "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}},
    }


def test_제목은_타입으로_찾는다():
    # 제목 프로퍼티의 이름은 데이터베이스마다 다르고 타입만 title로 고정이다.
    assert node_title(PAGE) == "Sidekiq 큐 지연 대응"


def test_데이터소스_제목은_최상위_필드에_있다():
    data_source = {
        "object": "data_source",
        "id": "d1",
        "title": [{"plain_text": "회의록"}],
    }
    assert node_title(data_source) == "회의록"


def test_데이터베이스_제목도_최상위_필드에_있다():
    # 워크스페이스 직속 데이터베이스가 최상위가 되면 이 이름이 출처로 찍힌다.
    database = {
        "object": "database",
        "id": "db",
        "title": [{"plain_text": "회의록"}],
    }
    assert node_title(database) == "회의록"


def test_제목이_비어_있으면_대체_문구를_쓴다():
    assert node_title({"properties": {"Name": {"type": "title", "title": []}}}) == (
        "제목 없음"
    )


def test_부모는_type이_가리키는_키에서_읽는다():
    assert parent_ref(PAGE) == ("page_id", "0ef104cd-477e-80e1-8571-cfd10e92339a")
    assert parent_ref(
        {"parent": {"type": "data_source_id", "data_source_id": "d1"}}
    ) == (("data_source_id", "d1"))


def test_워크스페이스_직속은_부모가_없다():
    # 값이 ID가 아니라 true라 그대로 쓰면 true가 최상위로 잡힌다.
    assert parent_ref({"parent": {"type": "workspace", "workspace": True}}) is None


def test_권한_밖으로_나가는_자리가_최상위다():
    # 최상위의 부모는 통합이 볼 수 없어 search에 나오지 않는다.
    nodes = [
        _page("hq", {"type": "page_id", "page_id": "팀스페이스"}),
        _page("a", {"type": "page_id", "page_id": "hq"}),
        _page("b", {"type": "page_id", "page_id": "a"}),
    ]

    assert derive_roots(nodes) == {"hq": "hq", "a": "hq", "b": "hq"}


def test_데이터베이스_행은_데이터베이스를_넘어_팀스페이스까지_올라간다():
    # search가 database를 안 돌려줘서 데이터소스에서 사슬이 끊긴다.
    nodes = [
        _page("hq", {"type": "workspace", "workspace": True}),
        {
            "object": "data_source",
            "id": "d1",
            "title": [{"plain_text": "회의록"}],
            "parent": {"type": "database_id", "database_id": "db1"},
        },
        _page("row", {"type": "data_source_id", "data_source_id": "d1"}),
    ]

    roots = derive_roots(
        nodes,
        fetch_node=lambda kind, i: _page("db", {"type": "page_id", "page_id": "hq"}),
    )
    assert roots["row"] == "hq"


def test_블록_안에_있는_페이지는_블록을_풀어_올라간다():
    # 실측에서 문서 페이지 1,769개 중 545개가 이 경우였다.
    nodes = [
        _page("hq", {"type": "workspace", "workspace": True}),
        _page("a", {"type": "block_id", "block_id": "토글"}),
    ]

    roots = derive_roots(
        nodes,
        fetch_node=lambda kind, i: _page("db", {"type": "page_id", "page_id": "hq"}),
    )
    assert roots["a"] == "hq"


def test_못_풀면_거기서_멈춘다():
    nodes = [_page("a", {"type": "block_id", "block_id": "토글"})]

    assert derive_roots(nodes)["a"] == "a"


def test_워크스페이스_직속_데이터베이스가_최상위가_된다():
    # "회의록"이 그렇다. 조회 실패와 "부모가 워크스페이스라 없음"을 같은
    # None으로 뭉뚱그리면 그 아래가 전부 엉뚱한 자리에 붙는다.
    nodes = [_page("row", {"type": "database_id", "database_id": "db"})]
    database = {
        "object": "database",
        "id": "db",
        "title": [{"plain_text": "회의록"}],
        "parent": {"type": "workspace", "workspace": True},
    }

    assert derive_roots(nodes, fetch_node=lambda kind, i: database)["row"] == "db"


def test_데이터베이스_행_여부():
    assert is_database_row(
        _page("r", {"type": "data_source_id", "data_source_id": "d"})
    )
    assert not is_database_row(_page("p", {"type": "page_id", "page_id": "hq"}))


def test_최상위가_여럿이면_각자에_붙는다():
    nodes = [
        _page("hq1", {"type": "workspace", "workspace": True}),
        _page("hq2", {"type": "workspace", "workspace": True}),
        _page("a", {"type": "page_id", "page_id": "hq2"}),
    ]

    roots = derive_roots(nodes)
    assert roots["a"] == "hq2"
    assert sorted(set(roots.values())) == ["hq1", "hq2"]


def test_부모가_서로를_가리켜도_멈춘다():
    # 노션이 이런 응답을 줄 일은 없지만 무한 루프로 갚을 일도 아니다.
    nodes = [
        _page("x", {"type": "page_id", "page_id": "y"}),
        _page("y", {"type": "page_id", "page_id": "x"}),
    ]

    assert set(derive_roots(nodes)) == {"x", "y"}


def test_사람은_이메일로_적는다():
    assert author_of(PAGE["created_by"], EMAILS) == "lch@team-mono.com"


def test_매핑에_없으면_봇으로_적는다():
    assert author_of({"id": "1edc05f6-2702"}, EMAILS) == "bot:1edc05f6-2702"


def test_페이지를_item_행으로_바꾼다():
    row = build_page_row(3, PAGE, "워커를 늘려 해소했다.", 900, EMAILS)

    assert row["data_source_id"] == 3
    assert row["external_id"] == PAGE["id"]
    assert row["url"] == PAGE["url"]
    assert row["title"] == "Sidekiq 큐 지연 대응"
    assert row["author"] == "lch@team-mono.com"
    assert row["source_updated_at"].astimezone(timezone.utc).hour == 5
    assert row["distill_state"] == "pending"
    assert (row["distill_after"] - row["source_updated_at"]).total_seconds() == 900


def test_제목을_본문_앞에_붙인다():
    # 제목에만 있는 말은 raw_text에 없으면 어휘 검색으로 영영 걸리지 않는다.
    row = build_page_row(3, PAGE, "본문", 900, EMAILS)

    assert row["raw_text"].startswith("Sidekiq 큐 지연 대응\n\n")
    assert "본문" in row["raw_text"]


def test_마지막_편집자를_metadata에_남긴다():
    row = build_page_row(3, PAGE, "본문", 900, EMAILS)

    assert json.loads(row["metadata"])["last_edited_by"] == "byb@team-mono.com"
