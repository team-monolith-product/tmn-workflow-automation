"""
로봇에게 예약해 둔 작업(scheduled_task)의 규칙과 저장을 맡습니다.

예약 하나가 한 행입니다. 언제 도는지(한 번·반복·끝)를 정하는 규칙도 여기 둡니다 --
꺼낼 때 반복 예약을 다음 회차로 옮기려면 저장하는 쪽이 그 규칙을 알아야 합니다.

함수마다 커넥션을 열고 닫습니다. psycopg 는 동기라 이벤트 루프에서는
asyncio.to_thread 로 부릅니다.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from service.db import connect, fetch_all, fetch_one

KST = ZoneInfo("Asia/Seoul")

# 너무 먼 예약은 잊힌 채로 남는다. 그보다 먼 일은 노션 작업으로 남긴다.
MAX_AHEAD = timedelta(days=366)

# 반복 예약의 최소 간격. 크론식을 잘못 써서 매분 채널에 글이 올라가는 것을 막는다.
MIN_INTERVAL = timedelta(hours=1)

# 한 번에 꺼내는 최대 개수. 실행은 각자 따로 돌므로 폴러를 오래 잡지 않는다.
CLAIM_LIMIT = 20

# 한 채널에 걸어 둘 수 있는 대기 예약 수. 에이전트가 반복 예약을 잘못 이해해
# 수백 개를 거는 것을 막는다.
MAX_PENDING_PER_CHANNEL = 50


class ScheduleProblem(Exception):
    """예약 인자가 잘못됐습니다. 메시지는 에이전트에게 그대로 돌려줄 안내문입니다."""


@dataclass(frozen=True)
class Schedule:
    """언제 도는지. cron 이 None 이면 first 에 한 번만 돕니다."""

    first: datetime
    cron: str | None = None
    until: datetime | None = None


@dataclass(frozen=True)
class ScheduledTask:
    """scheduled_task 한 행 중 실행과 목록에 필요한 열."""

    id: int
    channel_id: str
    thread_ts: str
    created_by: str
    prompt: str
    # 다음(꺼낸 뒤에는 이번) 회차 시각
    run_at: datetime
    # KST 크론식. 한 번만이면 None
    cron: str | None
    # 반복을 멈출 시각. 없으면 None
    until: datetime | None


@dataclass(frozen=True)
class Claim:
    """꺼낸 회차 하나. 반복 예약이면 표는 이미 next_run_at 으로 옮겨져 있습니다."""

    task: ScheduledTask
    # 다음 회차. 한 번짜리거나 마지막 회차면 None
    next_run_at: datetime | None


_COLUMNS = "id, channel_id, thread_ts, created_by, prompt, run_at, cron, until"


def format_time(when: datetime) -> str:
    """시각을 KST 로 보여줍니다."""
    return when.astimezone(KST).strftime("%Y-%m-%d(%a) %H:%M")


def next_run(
    cron: str, after: datetime, until: datetime | None = None
) -> datetime | None:
    """크론식의 after 뒤 첫 시각을 돌려줍니다. after 와 같은 시각은 넣지 않습니다.

    Args:
        cron: KST 5필드 크론식
        after: 기준 시각 (시간대 포함)
        until: 이 시각을 넘으면 None

    Returns:
        datetime | None: 다음 시각. until 을 넘으면 None
    """
    trigger = CronTrigger.from_crontab(cron, timezone=KST)
    when = trigger.get_next_fire_time(None, after + timedelta(seconds=1))
    if until is not None and when > until:
        return None
    return when


def _parse_time(text: str, name: str) -> datetime:
    """'YYYY-MM-DD HH:MM' 을 읽습니다. 시간대가 없으면 KST 입니다."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ScheduleProblem(
            f"{name} '{text}' 을 읽지 못했습니다. 'YYYY-MM-DD HH:MM' 으로 주세요."
        )
    return when if when.tzinfo else when.replace(tzinfo=KST)


def _parse_until(text: str) -> datetime:
    """끝 날짜나 시각을 읽습니다. 날짜만 주면 그날 끝까지입니다."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return _parse_time(text, "until")
    return datetime.combine(day, time(23, 59, 59), tzinfo=KST)


def _resolve_repeat(repeat: str, until: str | None, now: datetime) -> Schedule:
    fields = repeat.split()
    # APScheduler 는 요일 숫자를 0=월요일로 읽는다. 크론식의 0=일요일과 하루씩
    # 어긋나므로 숫자를 받지 않는다.
    if len(fields) == 5 and any(c.isdigit() for c in fields[4]):
        raise ScheduleProblem(
            "요일은 숫자 대신 mon,tue,wed,thu,fri,sat,sun 이름으로 주세요."
        )
    try:
        first = next_run(repeat, now)
    except ValueError as error:
        raise ScheduleProblem(
            f"repeat '{repeat}' 을 읽지 못했습니다({error})."
            " '분 시 일 월 요일' 5필드 크론식으로 주세요. 예: 매주 수요일 16시 '0 16 * * wed'"
        )
    if next_run(repeat, first) - first < MIN_INTERVAL:
        raise ScheduleProblem(f"반복 간격은 {MIN_INTERVAL} 이상이어야 합니다.")
    end = _parse_until(until) if until is not None else None
    if end is not None and end < first:
        raise ScheduleProblem(
            f"until {format_time(end)} 전에는 한 번도 돌지 않습니다(첫 회차 {format_time(first)})."
        )
    return Schedule(first, repeat, end)


def _resolve_once(
    run_at: str | None, in_minutes: int | None, now: datetime
) -> Schedule:
    if (run_at is None) == (in_minutes is None):
        raise ScheduleProblem("run_at, in_minutes, repeat 중 하나만 주세요.")
    if in_minutes is not None:
        if in_minutes < 1:
            raise ScheduleProblem("in_minutes 는 1 이상이어야 합니다.")
        when = now + timedelta(minutes=in_minutes)
    else:
        when = _parse_time(run_at, "run_at")
    if when <= now:
        raise ScheduleProblem(f"{format_time(when)} 은 이미 지났습니다.")
    if when - now > MAX_AHEAD:
        raise ScheduleProblem("1년 넘게 뒤로는 예약하지 않습니다.")
    return Schedule(when)


def resolve_schedule(
    run_at: str | None,
    in_minutes: int | None,
    repeat: str | None,
    until: str | None,
    now: datetime,
) -> Schedule:
    """예약 도구의 시각 인자를 하나의 Schedule 로 정합니다.

    Args:
        run_at: 한 번, 절대 시각. 시간대가 없으면 KST
        in_minutes: 한 번, 지금부터 몇 분 뒤
        repeat: 반복, KST 5필드 크론식
        until: 반복을 멈출 날짜('YYYY-MM-DD', 그날 끝까지)나 시각
        now: 지금 시각 (시간대 포함)

    Returns:
        Schedule: 첫 실행 시각과 반복 규칙

    Raises:
        ScheduleProblem: 인자가 잘못됨. 지금 시각을 덧붙인 안내문
    """
    try:
        if repeat is not None:
            if run_at is not None or in_minutes is not None:
                raise ScheduleProblem("run_at, in_minutes, repeat 중 하나만 주세요.")
            return _resolve_repeat(repeat, until, now)
        if until is not None:
            raise ScheduleProblem("until 은 repeat 와 함께만 씁니다.")
        return _resolve_once(run_at, in_minutes, now)
    except ScheduleProblem as problem:
        raise ScheduleProblem(f"{problem} 지금은 {format_time(now)} KST 입니다.")


def create(
    channel_id: str, thread_ts: str, created_by: str, prompt: str, schedule: Schedule
) -> ScheduledTask | None:
    """예약을 넣습니다.

    Args:
        channel_id: 결과를 올릴 채널
        thread_ts: 결과를 올릴 스레드
        created_by: 예약한 사람의 Slack 사용자 ID
        prompt: 시각이 되면 에이전트에게 줄 요청
        schedule: 언제 도는지

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
            (
                channel_id,
                thread_ts,
                created_by,
                prompt,
                schedule.first,
                schedule.cron,
                schedule.until,
            ),
        )
    return ScheduledTask(**row)


def list_pending(channel_id: str) -> list[ScheduledTask]:
    """채널의 대기 중인 예약을 다음 실행 시각 순으로 돌려줍니다.

    반복 예약은 회차가 도는 중에도 다음 회차로 대기 중이라 늘 보입니다.

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
    """대기 중인 예약을 취소합니다. 반복 예약은 회차가 도는 중에도 취소됩니다.

    다른 채널의 예약은 취소하지 못합니다. DM 예약은 그 DM 에서만 보입니다.

    Args:
        task_id: 예약 ID
        channel_id: 취소를 요청한 채널

    Returns:
        bool: 취소했으면 True. 없거나, 다른 채널이거나, 이미 끝났으면 False
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


def claim_due(now: datetime) -> list[Claim]:
    """시각이 된 대기 예약에서 이번 회차를 꺼냅니다.

    반복 예약은 꺼내는 자리에서 다음 회차로 옮기고 대기로 둡니다. 이번 회차가
    도중에 죽어도 다음 회차는 돌고, 도는 중에도 취소할 수 있습니다. 한 번짜리와
    마지막 회차만 running 이 됩니다. 파드가 여럿이어도 SKIP LOCKED 로 한 회차는
    한 쪽만 맡습니다.

    Args:
        now: 지금 시각 (시간대 포함)

    Returns:
        list[Claim]: 꺼낸 회차. 최대 CLAIM_LIMIT 개
    """
    with connect() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT {_COLUMNS} FROM scheduled_task
            WHERE status = 'pending' AND run_at <= %s
            ORDER BY run_at, id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (now, CLAIM_LIMIT),
        )
        claims = []
        for row in rows:
            task = ScheduledTask(**row)
            # 서버가 꺼져 밀린 회차는 한 번만 돌고 지금 뒤로 넘어간다.
            following = (
                next_run(task.cron, max(now, task.run_at), task.until)
                if task.cron
                else None
            )
            conn.execute(
                "UPDATE scheduled_task"
                " SET status = %s, run_at = coalesce(%s, run_at), started_at = %s"
                " WHERE id = %s",
                ("pending" if following else "running", following, now, task.id),
            )
            claims.append(Claim(task, following))
    return claims


def finish(task_id: int, error: str | None) -> None:
    """회차를 끝냅니다.

    마지막 에러를 남깁니다(성공이면 지웁니다). running 이던 한 번짜리·마지막
    회차는 done 이나 failed 로 끝냅니다. 반복 예약은 이미 다음 회차로 대기
    중이라 상태를 건드리지 않습니다.

    Args:
        task_id: 예약 ID
        error: 실패했으면 그 이유. 성공이면 None
    """
    with connect() as conn:
        conn.execute(
            """
            UPDATE scheduled_task
            SET last_error = %(error)s,
                finished_at = CASE WHEN status = 'running' THEN now() ELSE finished_at END,
                status = CASE WHEN status = 'running' THEN %(final)s ELSE status END
            WHERE id = %(id)s
            """,
            {"error": error, "final": "failed" if error else "done", "id": task_id},
        )
