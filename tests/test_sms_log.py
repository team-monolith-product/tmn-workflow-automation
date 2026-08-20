"""발송 기록의 중복 차단을 실제 Postgres 로 고정한다.

이 설계의 하중은 전부 부분 UNIQUE 인덱스 하나가 받는다. 가짜로만 검증하면
가짜와 스키마가 갈라져도 초록이 유지된다. KNOWLEDGE_DATABASE_URL 이 있으면
실제 DB 에서 돌고, 없으면 건너뛴다.

테이블은 트랜잭션 안에서 만들고 롤백하므로 남는 것이 없다.
"""

import os
import pathlib

import pytest

psycopg = pytest.importorskip("psycopg")

DDL = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "knowledge"
    / "003_sms_send.sql"
)

INSERT = """
INSERT INTO sms_send (campaign, phone, status, content)
SELECT %(campaign)s, phone, %(status)s, 'x'
FROM unnest(%(phones)s::text[]) AS phone
ON CONFLICT DO NOTHING
RETURNING phone
"""


@pytest.fixture
def conn():
    """마이그레이션을 적용한 트랜잭션. 끝나면 롤백한다."""
    dsn = os.environ.get("KNOWLEDGE_DATABASE_URL")
    if not dsn:
        pytest.skip("KNOWLEDGE_DATABASE_URL 이 없어 실제 DB 검증을 건너뜁니다")
    connection = psycopg.connect(dsn)
    connection.autocommit = False
    connection.execute(DDL.read_text(encoding="utf-8"))
    yield connection
    connection.rollback()
    connection.close()


def _claim(conn, campaign, phones, status="발송"):
    return [
        row[0]
        for row in conn.execute(
            INSERT, {"campaign": campaign, "phones": phones, "status": status}
        ).fetchall()
    ]


def test_같은_캠페인은_한_번만_들어간다(conn):
    assert _claim(conn, "discord", ["010", "020"]) == ["010", "020"]
    assert _claim(conn, "discord", ["010", "020", "030"]) == ["030"]


def test_다른_캠페인이면_같은_번호도_들어간다(conn):
    _claim(conn, "discord", ["010"])
    assert _claim(conn, "notice", ["010"]) == ["010"]


def test_CS는_같은_번호가_여러_번_들어간다(conn):
    # campaign 이 NULL 이면 부분 인덱스가 걸리지 않는다. 컬럼 모델에서 CS 를
    # 예외로 빼야 했던 이유가 여기서 사라진다.
    assert _claim(conn, None, ["010"]) == ["010"]
    assert _claim(conn, None, ["010"]) == ["010"]


def test_실패한_건은_재시도가_열린다(conn):
    _claim(conn, "discord", ["010"])
    conn.execute("UPDATE sms_send SET status = '실패' WHERE phone = '010'")

    assert _claim(conn, "discord", ["010"]) == ["010"]


def test_발송중은_재시도를_막는다(conn):
    # 타임아웃으로 굳은 건. 사람이 뿌리오 웹에서 확인해야 풀린다.
    _claim(conn, "discord", ["010"], status="발송중")

    assert _claim(conn, "discord", ["010"]) == []


def test_모르는_상태는_거절한다(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _claim(conn, "discord", ["010"], status="도달")
