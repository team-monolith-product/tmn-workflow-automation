"""노션 웹훅 이벤트 처리 테스트"""

import hashlib
import hmac

from service.knowledge.notion_events import (
    DELETE_EVENTS,
    INGEST_EVENTS,
    resolve_root,
    verify_signature,
)

# 실제 토큰 형식을 쓰지 않는다. 시크릿 스캐너가 막고, HMAC은 아무 문자열로도
# 검증된다.
TOKEN = "검증-토큰"
BODY = b'{"type":"page.content_updated","entity":{"id":"p1","type":"page"}}'


def _sign(body: bytes, token: str = TOKEN) -> str:
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def _page(page_id: str, parent: dict) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "parent": parent,
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "제목"}]}},
    }


def test_서명이_맞으면_통과한다():
    assert verify_signature(BODY, _sign(BODY), TOKEN)


def test_본문이_한_바이트라도_다르면_막는다():
    # 파싱한 뒤 다시 직렬화하면 공백과 키 순서가 달라져 여기서 걸린다.
    assert not verify_signature(BODY + b" ", _sign(BODY), TOKEN)


def test_토큰이_다르면_막는다():
    assert not verify_signature(BODY, _sign(BODY, "다른토큰"), TOKEN)


def test_서명_헤더가_없으면_막는다():
    assert not verify_signature(BODY, None, TOKEN)


def test_삭제와_적재_이벤트는_겹치지_않는다():
    assert not (INGEST_EVENTS & DELETE_EVENTS)


def test_워크스페이스_직속_페이지는_자기가_최상위다():
    page = _page("hq", {"type": "workspace", "workspace": True})

    assert resolve_root(page, lambda kind, i: None, {})["id"] == "hq"


def test_사슬을_API로_타고_올라간다():
    # 이벤트에는 페이지 하나만 오므로 조상은 전부 받아와야 한다.
    page = _page("row", {"type": "data_source_id", "data_source_id": "d1"})
    ancestors = {
        "d1": {
            "object": "data_source",
            "id": "d1",
            "title": [{"plain_text": "회의록"}],
            "parent": {"type": "database_id", "database_id": "db1"},
        },
        "db1": {
            "object": "database",
            "id": "db1",
            "title": [{"plain_text": "회의록"}],
            "parent": {"type": "workspace", "workspace": True},
        },
    }
    cache: dict = {}

    def fetch_node(kind, node_id):
        cache[node_id] = ancestors.get(node_id)
        return cache[node_id]

    root = resolve_root(page, fetch_node, cache)
    assert root["id"] == "db1"


def test_받아온_조상에서_멈춰도_최상위가_아니다():
    # 블록까지는 받았는데 그 위가 막힌 경우다. 부모가 남아 있으므로 최상위가
    # 아니고, 이것으로 data_source를 만들면 이름 없는 출처가 된다.
    page = _page("a", {"type": "block_id", "block_id": "b1"})
    block = {
        "object": "block",
        "id": "b1",
        "parent": {"type": "block_id", "block_id": "b2"},
    }
    cache: dict = {}

    def fetch_node(kind, node_id):
        cache[node_id] = block if node_id == "b1" else None
        return cache[node_id]

    assert resolve_root(page, fetch_node, cache) is None


def test_사슬이_끊기면_최상위가_없다():
    # 마지막으로 아는 노드가 최상위가 되는데 그것이 캐시에 없는 블록이면
    # 이름도 실체도 없는 출처가 된다. 넣지 않는다.
    page = _page("a", {"type": "block_id", "block_id": "토글"})

    assert resolve_root(page, lambda kind, i: None, {}) is None
