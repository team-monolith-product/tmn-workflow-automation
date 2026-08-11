"""
정제 워커 SQL 의 상태 전이 테스트 — 실제 Postgres 가 있을 때만 돕니다.

MARK_ERROR 의 CASE·coalesce·+1·RETURNING 은 이 브랜치에서 가장 틀리기 쉬운
부분인데, mock 으로는 "fetchone 결과를 그대로 돌려준다"밖에 검증할 수 없습니다.
coalesce 를 빠뜨리면 키가 없는 행(백로그 1.4만 건 전부)에서 NULL >= 3 이
UNKNOWN 이 되어 항상 pending 으로 빠지고, 실패한 건이 MAX_ATTEMPTS 에 영원히
닿지 못한 채 30분마다 무한 재시도하며 큐 앞을 막습니다. 그걸 mock 은 못 잡습니다.

    KNOWLEDGE_TEST_DATABASE_URL=postgresql://... pytest tests/test_knowledge_distill_sql.py
"""

import os

import pytest

from service.knowledge import distill

pytestmark = pytest.mark.skipif(
    not os.environ.get("KNOWLEDGE_TEST_DATABASE_URL"),
    reason="KNOWLEDGE_TEST_DATABASE_URL 이 없음 (SQL 테스트는 실제 Postgres 필요)",
)


@pytest.fixture
def conn():
    """테스트 전용 커넥션. 끝나면 전부 되돌립니다."""
    import psycopg
    from psycopg.rows import dict_row

    connection = psycopg.connect(
        os.environ["KNOWLEDGE_TEST_DATABASE_URL"], row_factory=dict_row
    )
    yield connection
    connection.rollback()
    connection.close()


@pytest.fixture
def item_id(conn):
    """정제 대기 상태인 item 한 행. 커밋하지 않습니다."""
    source = conn.execute("""
        INSERT INTO data_source (kind, external_id, name)
        VALUES ('slack', 'TEST_SQL', '테스트')
        ON CONFLICT (kind, external_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """).fetchone()["id"]
    row = conn.execute(
        """
        INSERT INTO item (
            data_source_id, external_id, source_created_at,
            raw, raw_text, content_hash, distill_state, distill_after
        )
        VALUES (%(source)s, 'C1:1.0', now(), '{}', '원문', 'h1', 'pending', now())
        RETURNING id
        """,
        {"source": source},
    ).fetchone()
    return row["id"]


def test_실패는_한도에_닿을_때까지_pending으로_돌아온다(conn, item_id):
    # 첫 실패에 error 로 옮기면 429 한 번에 그 스레드가 영구히 사라진다.
    states = [distill.mark_error(conn, item_id, "429") for _ in range(3)]

    assert states == ["pending"] * (distill.MAX_ATTEMPTS - 1) + ["error"]

    row = conn.execute(
        "SELECT metadata, distill_state FROM item WHERE id = %(id)s", {"id": item_id}
    ).fetchone()
    assert row["metadata"]["distill_attempts"] == distill.MAX_ATTEMPTS
    assert row["metadata"]["distill_error"] == "429"


def test_시도_횟수가_없던_행도_1부터_센다(conn, item_id):
    # metadata 는 '{}' 기본값이라 백로그 전부가 이 경우다. coalesce 가 없으면
    # NULL + 1 이 NULL 이 되고 CASE 가 영원히 참이 되지 않는다.
    distill.mark_error(conn, item_id, "타임아웃")

    row = conn.execute(
        "SELECT metadata FROM item WHERE id = %(id)s", {"id": item_id}
    ).fetchone()
    assert row["metadata"]["distill_attempts"] == 1


def test_실패한_건은_바로_다시_잡히지_않는다(conn, item_id):
    # distill_after 를 안 미루면 같은 건이 다음 회차에 즉시 다시 잡혀
    # 큐 앞을 막는다.
    distill.mark_error(conn, item_id, "429")

    assert distill.fetch_pending(conn, 50) == []


def test_대기_건수는_잡아오는_조건과_같다(conn, item_id):
    # 두 쿼리가 갈라지면 로그의 "정제 대기 N건"이 실제 큐와 달라진다.
    assert distill.count_pending(conn) == len(distill.fetch_pending(conn, 1000))
