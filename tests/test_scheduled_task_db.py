"""예약 작업 store 의 SQL 을 실제 Postgres 에 돌려보는 테스트

SCHEDULED_TASK_TEST_DATABASE_URL 이 없으면 건너뜁니다. 테스트마다 스키마를 새로
만들어 008 마이그레이션을 적용하고, 끝나면 지웁니다.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from service import scheduled_task as store
from service.db import connect, fetch_one


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("SCHEDULED_TASK_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("SCHEDULED_TASK_TEST_DATABASE_URL이 필요합니다")
    schema = "scheduled_task_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setenv(
            "KNOWLEDGE_DATABASE_URL",
            make_conninfo(dsn, options=f"-c search_path={schema}"),
        )
        try:
            with connect() as conn:
                conn.execute(
                    (
                        Path(__file__).resolve().parents[1]
                        / "migrations/knowledge/008_scheduled_task.sql"
                    ).read_text()
                )
            yield
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def _status(task_id: int) -> str:
    with connect() as conn:
        return fetch_one(
            conn, "SELECT status FROM scheduled_task WHERE id = %s", (task_id,)
        )["status"]


def test_시각이_된_대기_예약만_한_번_맡고_끝내면_done_이_된다(database):
    now = datetime.now(UTC)
    due = store.create("C1", "1.1", "U1", "지금 할 일", now - timedelta(minutes=1))
    later = store.create("C1", "1.1", "U1", "나중 할 일", now + timedelta(hours=1))

    # When : 두 번 연달아 맡고, 맡은 예약을 끝내면
    first = store.claim_due()
    second = store.claim_due()
    store.finish(due.id, None, None)

    # Then : 시각이 된 것만 한 번 맡고, 나중 것은 대기로 남는다
    assert [t.id for t in first] == [due.id]
    assert second == []
    assert _status(due.id) == "done"
    assert _status(later.id) == "pending"


def test_취소는_같은_채널의_대기_예약만_된다(database):
    run_at = datetime.now(UTC) + timedelta(hours=1)
    task = store.create("C1", "1.1", "U1", "할 일", run_at)
    ran = store.create("C1", "1.1", "U1", "할 일", run_at)
    with connect() as conn:
        conn.execute(
            "UPDATE scheduled_task SET status = 'running' WHERE id = %s", (ran.id,)
        )

    # When : 다른 채널에서, 이미 실행 중인 것을, 같은 채널에서 취소하면
    other_channel = store.cancel(task.id, "C2")
    running = store.cancel(ran.id, "C1")
    same_channel = store.cancel(task.id, "C1")

    # Then : 같은 채널의 대기 예약만 취소되고 목록에서 빠진다
    assert (other_channel, running, same_channel) == (False, False, True)
    assert store.list_pending("C1") == []


def test_채널의_대기_예약이_한도면_더_넣지_않는다(database, monkeypatch):
    monkeypatch.setattr(store, "MAX_PENDING_PER_CHANNEL", 2)
    run_at = datetime.now(UTC) + timedelta(hours=1)

    # When : 한도만큼 넣은 뒤 하나 더 넣으면
    store.create("C1", "1.1", "U1", "a", run_at)
    store.create("C1", "1.1", "U1", "b", run_at)
    over = store.create("C1", "1.1", "U1", "c", run_at)
    other = store.create("C2", "1.1", "U1", "d", run_at)

    # Then : 그 채널에는 들어가지 않고, 다른 채널은 영향이 없다
    assert over is None
    assert other is not None


def test_반복_회차를_끝내면_다음_시각으로_옮겨_다시_대기한다(database):
    now = datetime.now(UTC)
    task = store.create(
        "C1", "1.1", "U1", "매주 공지", now - timedelta(minutes=1), "0 16 * * wed"
    )
    store.claim_due()

    # When : 실패한 회차를 다음 주 시각으로 끝내면
    next_week = now + timedelta(days=7)
    store.finish(task.id, "RuntimeError('x')", next_week)

    # Then : 다시 대기로 돌아가 목록에 다음 시각과 함께 보이고, 지금은 맡지 않는다
    assert _status(task.id) == "pending"
    assert [t.run_at for t in store.list_pending("C1")] == [next_week]
    assert store.claim_due() == []
