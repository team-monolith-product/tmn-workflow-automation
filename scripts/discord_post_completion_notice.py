"""
수업 종료 후 Discord 포럼 채널에 자동 공지 게시

두 단계로 동작:
1. schedule_today_notices(): 매일 아침 크론으로 실행.
   Google Sheets에서 오늘 일정을 읽고, 각 종료시각에 맞춰 APScheduler 1회성 job 등록.
2. post_notice(): 등록된 종료시각에 실행.
   해당 학교의 "{학교이름}-공지" 포럼 채널에 템플릿 기반 게시물 3개 작성.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

from api.discord import (
    create_message,
    create_thread,
    get_channel,
    get_guild_channels,
    get_message,
)
from api.google_sheets import get_worksheet_values

load_dotenv()

KST = timezone(timedelta(hours=9))
TIMEZONE = "Asia/Seoul"

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

DATE_PLACEHOLDER = "yymmdd"
FORUM_CHANNEL_TYPE = 15
SCHOOL_NAME_COL = 0

_scheduler = None


def set_scheduler(scheduler):
    """외부 스케줄러 인스턴스를 주입받는다."""
    global _scheduler
    _scheduler = scheduler


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
    for row in rows[1:]:  # 헤더 스킵
        school_name = str(row[SCHOOL_NAME_COL]).strip() if row else ""
        if not school_name:
            continue

        schedules = []
        col = 1
        while col + 2 < len(row):
            date_val = row[col]
            start_val = row[col + 1]
            end_val = row[col + 2]

            # 빈 슬롯 (학교당 최대 12슬롯 중 미사용)
            if date_val == "":
                col += 3
                continue

            date = SHEETS_EPOCH + timedelta(days=int(date_val))
            start_minutes = round(start_val * 1440)
            end_minutes = round(end_val * 1440)

            schedules.append({
                "date": date,
                "start_hour": start_minutes // 60,
                "start_min": start_minutes % 60,
                "end_hour": end_minutes // 60,
                "end_min": end_minutes % 60,
            })

            col += 3

        results.append({"school_name": school_name, "schedules": schedules})

    return results


def read_school_schedules() -> list[dict]:
    """시트에서 학교별 일정을 읽어온다."""
    rows = get_worksheet_values(SPREADSHEET_ID, WORKSHEET_ID)
    return parse_school_schedules(rows)


def find_forum_channel(guild_id: str, channel_name: str) -> dict | None:
    """서버에서 이름이 일치하는 포럼 채널을 찾는다."""
    channels = get_guild_channels(guild_id)
    for ch in channels:
        if ch.get("type") == FORUM_CHANNEL_TYPE and ch.get("name") == channel_name:
            return ch
    return None


def fetch_thread_template(thread_id: str) -> dict:
    """
    포럼 스레드의 제목과 시작 메시지 본문을 가져온다.
    포럼 스레드의 시작 메시지는 id가 thread_id와 동일하다.

    Returns:
        {"title": "yymmdd_간식증빙사진", "content": "-"}
    """
    channel = get_channel(thread_id)
    starter = get_message(thread_id, thread_id)
    return {"title": channel["name"], "content": starter["content"]}


def fetch_templates() -> list[dict]:
    """모든 템플릿 스레드의 제목/본문을 가져온다."""
    return [fetch_thread_template(tid) for tid in TEMPLATE_THREAD_IDS]


def post_notice(school_name: str, date_str: str):
    """
    특정 학교의 포럼 채널에 공지 게시물 3개를 작성한다.
    APScheduler에 의해 종료시각에 호출된다.

    Args:
        school_name: 학교명
        date_str: 날짜 문자열 (예: "260531")
    """
    channel_name = f"{school_name}-공지"
    print(f"[post_notice] {channel_name} 공지 시작")

    channel = find_forum_channel(GUILD_ID, channel_name)
    if not channel:
        msg = f"[채널 없음] {channel_name}"
        print(f"  {msg}")
        if LOG_CHANNEL_ID:
            create_message(LOG_CHANNEL_ID, msg)
        return

    templates = fetch_templates()

    for tmpl in templates:
        new_title = tmpl["title"].replace(DATE_PLACEHOLDER, date_str)
        create_thread(channel["id"], name=new_title, content=tmpl["content"])
        print(f"  게시물 생성: {new_title}")

    print(f"[post_notice] {channel_name} 공지 완료")


def schedule_today_notices(scheduler=None):
    """
    오늘 수업이 있는 학교를 찾아 종료시각에 맞춰 1회성 job을 등록한다.
    매일 아침 크론으로 호출된다.
    """
    sched = scheduler or _scheduler
    if not sched:
        print("[schedule_today_notices] 스케줄러가 없습니다.")
        return

    now = datetime.now(KST)
    today = now.date()
    today_str = now.strftime("%y%m%d")

    print(f"[schedule_today_notices] {today} 일정 등록 시작")

    schools = read_school_schedules()
    registered = 0

    for school in schools:
        for s in school["schedules"]:
            if s["date"].date() != today:
                continue

            end_time = s["date"].replace(hour=s["end_hour"], minute=s["end_min"])

            # 이미 지난 시각은 스킵
            if end_time <= now:
                continue

            job_id = f"discord_notice_{school['school_name']}_{today_str}_{s['end_hour']:02d}{s['end_min']:02d}"

            sched.add_job(
                post_notice,
                trigger=DateTrigger(run_date=end_time, timezone=TIMEZONE),
                id=job_id,
                name=job_id,
                args=[school["school_name"], today_str],
                replace_existing=True,
            )
            print(f"  등록: {school['school_name']} → {end_time.strftime('%H:%M')}")
            registered += 1

    print(f"[schedule_today_notices] {registered}개 job 등록 완료")


def main(dry_run: bool = False, target_date: str | None = None):
    """CLI용 엔트리포인트. dry-run 및 날짜 지정 테스트 지원."""
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
        target = datetime(year, month, day, tzinfo=KST).date()
    else:
        target = now.date()

    today_str = target.strftime("%y%m%d")

    print(f"[discord_post_completion_notice] 실행: {target}")

    schools = read_school_schedules()
    print(f"  학교 수: {len(schools)}")

    notices = []
    for school in schools:
        for s in school["schedules"]:
            if s["date"].date() != target:
                continue
            end_time = s["date"].replace(hour=s["end_hour"], minute=s["end_min"])
            notices.append((school["school_name"], end_time))

    if not notices:
        print("  공지 대상 학교 없음")
        return

    if dry_run:
        for school_name, end_time in notices:
            channel_name = f"{school_name}-공지"
            print(f"  [DRY-RUN] {end_time.strftime('%H:%M')} → {channel_name}")
            print(f"    → {today_str}_간식증빙사진")
            print(f"    → {today_str}_출석부사진")
            print(f"    → {today_str}_수업사진")
        return

    for school_name, _ in notices:
        post_notice(school_name, today_str)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="수업 종료 후 Discord 공지 자동 게시")
    parser.add_argument(
        "--dry-run", action="store_true", help="실제 전송 없이 콘솔 출력만"
    )
    parser.add_argument("--date", type=str, help="대상 날짜 (YYYY-MM-DD 또는 MM-DD)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, target_date=args.date)
