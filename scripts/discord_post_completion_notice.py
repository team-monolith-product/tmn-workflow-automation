"""
수업 종료 후 Discord 포럼 채널에 자동 공지 게시

10분마다 실행되며:
1. Google Sheets에서 학교별 수업 일정을 읽음
2. 오늘 자정 ~ now 윈도우에 종료시각이 들어가는 학교를 감지
   (outage 후 복귀 시 그날 안이면 자동 catch-up)
3. "{학교이름}-공지" 포럼 채널에 템플릿 기반 게시물 3개 작성
4. 멱등성: 같은 제목의 활성 스레드가 이미 있으면 그 템플릿만 스킵 (per-template)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from datetime import datetime, timedelta, timezone

import redis
from dotenv import load_dotenv

from api.discord import (
    create_message,
    create_thread,
    get_active_threads,
    get_channel,
    get_guild_channels,
    get_message,
)
from api.google_sheets import get_worksheet_values

load_dotenv()

KST = timezone(timedelta(hours=9))

# Google Sheets 시리얼 날짜 기준일 (1899-12-30)
SHEETS_EPOCH = datetime(1899, 12, 30, tzinfo=KST)

SPREADSHEET_ID = os.environ.get(
    "GOOGLE_SPREADSHEET_ID", "1ZP7wyxbwRO2AhyzForm7494C6bMbY_HAeNHV6-WbSnc"
)
WORKSHEET_ID = int(os.environ.get("GOOGLE_WORKSHEET_ID", "451387449"))
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1504338147155251220")
TEMPLATE_THREAD_IDS = [
    tid.strip()
    for tid in os.environ.get("DISCORD_TEMPLATE_THREAD_IDS", "").split(",")
    if tid.strip()
]
LOG_CHANNEL_ID = os.environ.get("DISCORD_LOG_CHANNEL_ID", "1505927819094397038")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
FORUM_CHANNEL_TYPE = 15
SCHOOL_NAME_COL = 0

# 채널 없음 알림 키 TTL (36시간)
NO_CHANNEL_NOTIFIED_TTL = 36 * 60 * 60


def parse_school_schedules(rows: list[list]) -> list[dict]:
    """
    UNFORMATTED_VALUE로 읽은 시트 데이터를 학교별 일정 dict 리스트로 변환한다.

    시트 스키마: 학교명 | 날짜1 | 시작1 | 종료1 | 날짜2 | 시작2 | 종료2 | ...

    Returns:
        [
            {
                "school_name": "선인고등학교",
                "schedules": [
                    {"date": datetime, "start_hour": 13, "start_min": 30, "end_hour": 17, "end_min": 30},
                    ...
                ]
            },
            ...
        ]
    """
    results = []
    for row in rows[1:]:
        school_name = str(row[SCHOOL_NAME_COL]).strip() if row else ""
        if not school_name:
            continue

        schedules = []
        col = 1
        while col + 2 < len(row):
            date_val = row[col]
            start_val = row[col + 1]
            end_val = row[col + 2]

            # 빈 슬롯 또는 부분 입력된 슬롯은 스킵
            if date_val == "" or start_val == "" or end_val == "":
                col += 3
                continue

            date = SHEETS_EPOCH + timedelta(days=int(date_val))
            start_minutes = round(start_val * 1440)
            end_minutes = round(end_val * 1440)

            schedules.append(
                {
                    "date": date,
                    "start_hour": start_minutes // 60,
                    "start_min": start_minutes % 60,
                    "end_hour": end_minutes // 60,
                    "end_min": end_minutes % 60,
                }
            )

            col += 3

        results.append({"school_name": school_name, "schedules": schedules})

    return results


def read_school_schedules() -> list[dict]:
    """시트에서 학교별 일정을 읽어온다."""
    rows = get_worksheet_values(
        SPREADSHEET_ID, WORKSHEET_ID, value_render_option="UNFORMATTED_VALUE"
    )
    return parse_school_schedules(rows)


def find_forum_channel(channels: list[dict], channel_name: str) -> dict | None:
    """채널 목록에서 이름이 일치하는 포럼 채널을 찾는다."""
    for ch in channels:
        if ch.get("type") == FORUM_CHANNEL_TYPE and ch.get("name") == channel_name:
            return ch
    return None


def fetch_thread_template(thread_id: str) -> dict:
    """
    포럼 스레드의 제목과 시작 메시지 본문을 가져온다.
    포럼 스레드의 시작 메시지는 id가 thread_id와 동일하다.
    """
    channel = get_channel(thread_id)
    starter = get_message(thread_id, thread_id)
    return {"title": channel["name"], "content": starter["content"]}


def fetch_templates() -> list[dict]:
    """모든 템플릿 스레드의 제목/본문을 가져온다."""
    return [fetch_thread_template(tid) for tid in TEMPLATE_THREAD_IDS]


def get_redis_client() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )


def notify_missing_channel_once(
    redis_client: redis.Redis, school_name: str, today_str: str
) -> bool:
    """
    채널 없음 알림을 하루 1회로 제한.
    Returns: 알림을 새로 보냈으면 True, 이미 알린 적 있어 스킵했으면 False
    """
    key = f"discord_notice:no_channel:{today_str}:{school_name}"
    # SET ... NX EX ttl: 키가 없을 때만 set, 있으면 None 반환
    was_set = redis_client.set(key, "1", nx=True, ex=NO_CHANNEL_NOTIFIED_TTL)
    return bool(was_set)


def format_marker(end_time: datetime) -> str:
    """종료시각을 'M.d(E) HH:mm' 형식 문자열로 포맷."""
    weekday = KOREAN_WEEKDAYS[end_time.weekday()]
    return f"{end_time.month}.{end_time.day}({weekday}) {end_time:%H:%M}"


def make_title(template_title: str, end_time: datetime) -> str:
    """템플릿 제목 앞에 종료시각 prefix를 붙인다."""
    return f"{format_marker(end_time)} - {template_title}"


def main(dry_run: bool = False, target_date: str | None = None):
    """
    10분마다 호출되어 오늘 이미 종료된 수업의 학교에 공지를 게시한다.
    윈도우는 오늘 자정 ~ now. 멱등성으로 중복 방지하므로 outage 후 복귀 시 자동 catch-up.

    Args:
        dry_run: 실제 Discord 전송 없이 콘솔 출력만
        target_date: 테스트용 날짜 지정 (YYYY-MM-DD or MM-DD). 지정 시 그 날 하루 전체를 윈도우로.
    """
    now = datetime.now(KST)

    if target_date:
        parts = target_date.split("-")
        if len(parts) == 3:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            year = now.year
            month, day = int(parts[0]), int(parts[1])
        else:
            print(f"  잘못된 날짜 형식: {target_date} (YYYY-MM-DD 또는 MM-DD)")
            return
        now = datetime(year, month, day, 23, 59, 59, tzinfo=KST)

    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    print(f"[discord_post_completion_notice] 실행: {now.isoformat()}")
    print(
        f"  윈도우: [{window_start.strftime('%Y-%m-%d %H:%M')}, {now.strftime('%H:%M')}]"
    )

    schools = read_school_schedules()

    # 윈도우 안에 종료시각이 들어가는 (학교, end_time) 추출
    targets = []
    for school in schools:
        for s in school["schedules"]:
            end_time = s["date"].replace(hour=s["end_hour"], minute=s["end_min"])
            if window_start < end_time <= now:
                targets.append((school["school_name"], end_time))

    if not targets:
        print("  공지 대상 없음")
        return

    print(f"  대상 {len(targets)}건")

    if dry_run:
        for school_name, end_time in targets:
            channel_name = f"{school_name}-공지"
            marker = format_marker(end_time)
            print(f"  [DRY-RUN] {channel_name}")
            print(f"    → {marker} - <템플릿 제목>")
        return

    # 템플릿/채널/활성 스레드 한 번씩 조회
    templates = fetch_templates()
    channels = get_guild_channels(GUILD_ID)
    active_threads = get_active_threads(GUILD_ID).get("threads", [])

    today_str = now.strftime("%y%m%d")
    redis_client = get_redis_client()

    # 학교별 처리: 한 학교의 외부 호출 실패가 나머지 학교 처리를 막지 않도록 격리.
    # 다음 cron에서 멱등성으로 catch-up되지만, 실패 사실은 LOG 채널에 알려야 함.
    for school_name, end_time in targets:
        try:
            process_school(
                school_name,
                end_time,
                channels,
                active_threads,
                templates,
                redis_client,
                today_str,
            )
        except Exception as e:
            msg = f"[처리 실패] {school_name} {format_marker(end_time)}: {type(e).__name__}: {e}"
            print(f"  {msg}")
            if LOG_CHANNEL_ID:
                create_message(LOG_CHANNEL_ID, msg)


def process_school(
    school_name: str,
    end_time: datetime,
    channels: list[dict],
    active_threads: list[dict],
    templates: list[dict],
    redis_client: redis.Redis,
    today_str: str,
):
    """한 학교의 공지 게시를 처리한다."""
    channel_name = f"{school_name}-공지"
    channel = find_forum_channel(channels, channel_name)
    if not channel:
        print(f"  [채널 없음] {channel_name}")
        if LOG_CHANNEL_ID and notify_missing_channel_once(
            redis_client, school_name, today_str
        ):
            create_message(LOG_CHANNEL_ID, f"[채널 없음] {channel_name}")
        return

    channel_id = channel["id"]
    existing_titles = {
        t.get("name", "") for t in active_threads if t.get("parent_id") == channel_id
    }

    # 멱등성: 동일 제목 스레드가 이미 있으면 그 템플릿만 스킵.
    # 부분 실패 후 재시도 시 누락분만 생성하도록 per-template 검사.
    for tmpl in templates:
        new_title = make_title(tmpl["title"], end_time)
        if new_title in existing_titles:
            print(f"  스킵 (중복): {new_title}")
            continue
        create_thread(channel_id, name=new_title, content=tmpl["content"])
        print(f"  생성: {new_title}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="수업 종료 후 Discord 공지 자동 게시")
    parser.add_argument(
        "--dry-run", action="store_true", help="실제 전송 없이 콘솔 출력만"
    )
    parser.add_argument("--date", type=str, help="대상 날짜 (YYYY-MM-DD 또는 MM-DD)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, target_date=args.date)
