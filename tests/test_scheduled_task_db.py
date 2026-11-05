"""예약 작업 store 의 SQL 을 실제 Postgres 에 돌려보는 테스트

SCHEDULED_TASK_TEST_DATABASE_URL 이 없으면 건너뜁니다. 테스트마다 스키마를 새로
만들어 008 마이그레이션을 적용하고, 끝나면 지웁니다.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from service import scheduled_task as store
from service.db import connect, fetch_one
from service.scheduled_task import KST, Schedule, ScheduleProblem

# 수요일 오전 10시
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=KST)
WEEKLY = "0 16 * * wed"


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


def _create(prompt: str, schedule: Schedule, channel: str = "C1"):
    return store.create(channel, "1.1", "U1", prompt, schedule)


def _row(task_id: int) -> dict:
    with connect() as conn:
        return fetch_one(
            conn,
            "SELECT status, run_at, last_error FROM scheduled_task WHERE id = %s",
            (task_id,),
        )


def test_한_번짜리는_꺼내는_자리에서_done_이_돼_한_번만_맡는다(database):
    due = _create("지금", Schedule(NOW - timedelta(minutes=1)))
    later = _create("나중", Schedule(NOW + timedelta(hours=1)))

    # When : 두 번 연달아 꺼내면
    first = store.claim_due(NOW)
    second = store.claim_due(NOW)

    # Then : 시각이 된 것만 한 번 꺼내 바로 done 이 되고, 나중 것은 대기로 남는다
    assert [(c.task.id, c.next_run_at) for c in first] == [(due.id, None)]
    assert second == []
    assert _row(due.id)["status"] == "done"
    assert _row(later.id)["status"] == "pending"


def test_반복은_꺼내는_자리에서_다음_회차로_옮겨_대기하고_도는_중에도_취소된다(
    database,
):
    task = _create("매주 공지", Schedule(NOW - timedelta(minutes=1), WEEKLY))

    # When : 회차를 꺼내면
    [claim] = store.claim_due(NOW)

    # Then : 이번 회차 시각은 그대로 받고, 표는 오늘 16시로 옮겨져 대기 중이다
    next_wednesday = datetime(2026, 11, 4, 16, 0, tzinfo=KST)
    assert claim.task.run_at == NOW - timedelta(minutes=1)
    assert claim.next_run_at == next_wednesday
    assert _row(task.id) == {
        "status": "pending",
        "run_at": next_wednesday,
        "last_error": None,
    }

    # When : 회차가 도는 중에 취소하고, 그 뒤 회차가 실패로 끝나면
    canceled = store.cancel(task.id, "C1")
    store.record_result(task.id, "RuntimeError('x')")

    # Then : 취소가 되고, 끝난 회차가 취소를 되돌리지 않는다
    assert canceled is True
    assert _row(task.id)["status"] == "canceled"


def test_반복의_마지막_회차는_꺼낼_때_끝나고_실패는_이유로_남는다(database):
    task = _create(
        "마지막",
        Schedule(NOW - timedelta(minutes=1), WEEKLY, NOW + timedelta(hours=1)),
    )

    # When : until 전에 다음 회차가 없는 회차를 꺼내 실패로 기록하면
    [claim] = store.claim_due(NOW)
    store.record_result(task.id, "RuntimeError('x')")

    # Then : 다음 회차 없이 done 으로 끝나고 실패 이유가 남는다
    assert claim.next_run_at is None
    assert _row(task.id)["status"] == "done"
    assert _row(task.id)["last_error"] == "RuntimeError('x')"


def test_취소는_같은_채널의_대기_예약만_된다(database):
    task = _create("할 일", Schedule(NOW + timedelta(hours=1)))

    # When : 다른 채널에서 취소한 뒤 같은 채널에서 취소하면
    other_channel = store.cancel(task.id, "C2")
    same_channel = store.cancel(task.id, "C1")

    # Then : 같은 채널에서만 취소되고 목록에서 빠진다
    assert (other_channel, same_channel) == (False, True)
    assert store.list_pending("C1") == []


def test_채널의_대기_예약이_한도면_더_넣지_않는다(database, monkeypatch):
    monkeypatch.setattr(store, "MAX_PENDING_PER_CHANNEL", 2)
    schedule = Schedule(NOW + timedelta(hours=1))

    # When : 한도만큼 넣은 뒤 하나 더 넣고, 다른 채널에도 넣으면
    _create("a", schedule)
    _create("b", schedule)
    with pytest.raises(ScheduleProblem):
        _create("c", schedule)
    other = _create("d", schedule, channel="C2")

    # Then : 그 채널에는 들어가지 않고, 다른 채널은 영향이 없다
    assert other.channel_id == "C2"
