"""
노션 웹훅 이벤트를 지식베이스에 반영하는 Service Layer입니다.

색인은 여기로만 바뀝니다. 페이지가 바뀌면 다시 받아 덮고, 지워지거나 본문이
기준 밑으로 줄면 뺍니다.

이벤트 payload에는 본문이 없습니다. 엔티티 ID와 종류와 시각뿐이라 페이지와
블록은 API로 다시 받습니다.
"""

import hashlib
import hmac
from typing import Any, Callable

import psycopg

from service.knowledge.notion import derive_roots, parent_ref

# 페이지를 다시 받아 적재할 이벤트.
#
# 이벤트 종류 선택은 노션 연결 설정 화면에 있습니다. 여기 있는 것은 "왔을 때
# 어떻게 처리하는가"이지 "무엇을 받는가"가 아닙니다.
INGEST_EVENTS = {
    "page.created",
    "page.content_updated",
    "page.properties_updated",
    "page.moved",
    "page.undeleted",
}

# 색인에서 빼는 이벤트.
DELETE_EVENTS = {"page.deleted"}

# 노션 페이지 하나를 지웁니다. keep이 있으면 그 data_source에 붙은 행은
# 남깁니다. 페이지가 다른 최상위로 옮겨지면 (data_source_id, external_id)가
# 달라져 새 행이 생기는데, 옛 행을 그대로 두면 한 페이지가 두 출처에서 잡힙니다.
DELETE_PAGE = """
DELETE FROM item
USING data_source
WHERE item.data_source_id = data_source.id
  AND data_source.source = 'notion'
  AND item.external_id = %(external_id)s
  AND (%(keep)s::bigint IS NULL OR item.data_source_id <> %(keep)s)
"""


def verify_signature(body: bytes, signature: str | None, token: str) -> bool:
    """X-Notion-Signature를 검증합니다.

    본문을 받은 바이트 그대로 씁니다. 파싱한 뒤 다시 직렬화하면 공백과 키
    순서가 달라져 서명이 어긋납니다.

    Args:
        body: 요청 본문 원본
        signature: X-Notion-Signature 헤더. 없으면 None
        token: 구독을 만들 때 받은 verification_token

    Returns:
        bool: 서명이 맞으면 True
    """
    if not signature:
        return False
    expected = (
        "sha256=" + hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def resolve_root(
    page: dict[str, Any],
    fetch_node: Callable[[str, str], dict[str, Any] | None],
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    """페이지 하나의 최상위 노드를 찾습니다.

    derive_roots에 페이지 하나만 넘깁니다. 조상이 전부 캐시에 없으니 사슬을
    API로 타고 올라가는데, 그게 여기서 필요한 동작입니다.

    최상위는 부모가 없는 노드입니다. derive_roots는 사슬이 끊기면 마지막으로
    아는 노드를 돌려주므로, 부모가 남아 있는 것이 나오면 조상 조회가 실패한
    것이지 최상위를 찾은 것이 아닙니다. 그것으로 data_source를 만들면 이름도
    실체도 없는 출처가 검색 결과에 찍힙니다.

    Args:
        page: 노션 page 객체
        fetch_node: build_fetch_node 결과
        cache: fetch_node에 넘긴 것과 같은 캐시

    Returns:
        dict[str, Any] | None: 최상위 노드. 사슬이 끊겨 못 찾으면 None
    """
    root_id = derive_roots([page], fetch_node=fetch_node)[page["id"]]
    root = page if root_id == page["id"] else cache.get(root_id)
    if root is None or parent_ref(root) is not None:
        return None
    return root


def delete_page(
    conn: psycopg.Connection, external_id: str, keep: int | None = None
) -> int:
    """노션 페이지를 색인에서 지웁니다.

    Args:
        conn: 커넥션
        external_id: 노션 페이지 ID
        keep: 남길 data_source.id. None이면 전부 지웁니다

    Returns:
        int: 지운 행 수
    """
    with conn.cursor() as cur:
        cur.execute(DELETE_PAGE, {"external_id": external_id, "keep": keep})
        return cur.rowcount
