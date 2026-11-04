"""
로봇에게 예약해 둔 작업(scheduled_task)을 읽고 씁니다.

예약 하나가 한 행입니다. 상태 흐름과 죽은 running 을 다시 돌리지 않는 이유는
migrations/knowledge/008_scheduled_task.sql 에 있습니다.

함수마다 커넥션을 열고 닫습니다. psycopg 는 동기라 이벤트 루프에서는
asyncio.to_thread 로 부릅니다.
"""

from dataclasses import dataclass
from datetime import datetime

from service.db import connect, fetch_all, fetch_one


@dataclass(frozen=True)
class ScheduledTask:
    """scheduled_task 한 행 중 실행과 목록에 필요한 열."""

    id: int
    channel_id: str
    thread_ts: str
    created_by: str
    prompt: str
    run_at: datetime
    # KST 크론식. 한 번만이면 None
    cron: str | None
    # 반복을 멈출 시각. 없으면 None
    until: datetime | None


# 한 번에 꺼내는 최대 개수. 실행은 각자 따로 돌므로 폴러를 오래 잡지 않는다.
CLAIM_LIMIT = 20

# 한 채널에 걸어 둘 수 있는 대기 예약 수. 에이전트가 반복 예약을 잘못 이해해
# 수백 개를 거는 것을 막는다.
MAX_PENDING_PER_CHANNEL = 50

_COLUMNS = "id, channel_id, thread_ts, created_by, prompt, run_at, cron, until"


def create(
    channel_id: str,
    thread_ts: str,
    created_by: str,
    prompt: str,
    run_at: datetime,
    cron: str | None = None,
    until: datetime | None = None,
) -> ScheduledTask | None:
    """예약을 넣습니다.

    Args:
        channel_id: 결과를 올릴 채널
        thread_ts: 결과를 올릴 스레드
        created_by: 예약한 사람의 Slack 사용자 ID
        prompt: 시각이 되면 에이전트에게 줄 요청
        run_at: 첫 실행 시각. 시간대가 있어야 합니다.
        cron: 반복 주기(KST 크론식). 한 번만이면 None
        until: 반복을 멈출 시각. 없으면 None

    Returns:
        ScheduledTask | None: 넣은 예약. 채널의 대기 예약이 이미
            MAX_PENDING_PER_CHANNEL 개면 넣지 않고 None
    """
    with connect() as conn:
        # 같은 채널에서 동시에 넣어도 한도를 넘지 않게 채널 단위로 줄을 세운다.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('scheduled_task:' || %s))",
            (channel_id,),
        )
        pending = fetch_one(
            conn,
            "SELECT count(*) AS n FROM scheduled_task"
            " WHERE channel_id = %s AND status = 'pending'",
            (channel_id,),
        )
        if pending["n"] >= MAX_PENDING_PER_CHANNEL:
            return None
        row = fetch_one(
            conn,
            f"""
            INSERT INTO scheduled_task
                (channel_id, thread_ts, created_by, prompt, run_at, cron, until)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (channel_id, thread_ts, created_by, prompt, run_at, cron, until),
        )
    return ScheduledTask(**row)


def list_pending(channel_id: str) -> list[ScheduledTask]:
    """채널의 대기 중인 예약을 실행 시각 순으로 돌려줍니다.

    Args:
        channel_id: 채널 ID

    Returns:
        list[ScheduledTask]: 대기 중인 예약
    """
    with connect(read_only=True) as conn:
        rows = fetch_all(
            conn,
            f"SELECT {_COLUMNS} FROM scheduled_task"
            " WHERE channel_id = %s AND status = 'pending' ORDER BY run_at, id",
            (channel_id,),
        )
    return [ScheduledTask(**row) for row in rows]


def cancel(task_id: int, channel_id: str) -> bool:
    """대기 중인 예약을 취소합니다.

    다른 채널의 예약은 취소하지 못합니다. DM 예약은 그 DM 에서만 보입니다.

    Args:
        task_id: 예약 ID
        channel_id: 취소를 요청한 채널

    Returns:
        bool: 취소했으면 True. 없거나, 다른 채널이거나, 이미 실행됐으면 False
    """
    with connect() as conn:
        row = fetch_one(
            conn,
            "UPDATE scheduled_task SET status = 'canceled', finished_at = now()"
            " WHERE id = %s AND channel_id = %s AND status = 'pending'"
            " RETURNING id",
            (task_id, channel_id),
        )
    return row is not None


def claim_due() -> list[ScheduledTask]:
    """시각이 된 대기 예약을 running 으로 바꾸고 돌려줍니다.

    파드가 여럿이어도 SKIP LOCKED 로 한 예약은 한 쪽만 맡습니다.

    Returns:
        list[ScheduledTask]: 맡은 예약. 최대 CLAIM_LIMIT 개
    """
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            UPDATE scheduled_task SET status = 'running', started_at = now()
            WHERE id IN (
                SELECT id FROM scheduled_task
                WHERE status = 'pending' AND run_at <= now()
                ORDER BY run_at, id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING {_COLUMNS}
            """,
            (CLAIM_LIMIT,),
        )
    return sorted(
        (ScheduledTask(**row) for row in rows), key=lambda t: (t.run_at, t.id)
    )


def finish(task_id: int, error: str | None, next_run_at: datetime | None) -> None:
    """회차를 끝냅니다.

    다음 회차가 있으면 그 시각으로 옮겨 pending 으로 돌립니다. 실패했어도
    돌립니다 -- 한 번 실패했다고 매주 공지가 멈추면 안 됩니다.

    Args:
        task_id: 예약 ID
        error: 실패했으면 그 이유. 성공이면 None
        next_run_at: 다음 회차 시각. 한 번만이거나 반복이 끝났으면 None
    """
    with connect() as conn:
        if next_run_at is None:
            conn.execute(
                "UPDATE scheduled_task"
                " SET status = %s, last_error = %s, finished_at = now()"
                " WHERE id = %s AND status = 'running'",
                ("failed" if error else "done", error, task_id),
            )
        else:
            conn.execute(
                "UPDATE scheduled_task"
                " SET status = 'pending', run_at = %s, last_error = %s,"
                " finished_at = now()"
                " WHERE id = %s AND status = 'running'",
                (next_run_at, error, task_id),
            )
