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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

from api.discord import (
    create_forum_thread,
    fetch_thread_template,
    get_guild_channels,
    send_message,
)
from api.google_sheets import read_school_schedules

load_dotenv()

KST = timezone(timedelta(hours=9))
TIMEZONE = "Asia/Seoul"

SPREADSHEET_ID = os.environ.get(
    "GOOGLE_SPREADSHEET_ID", "1ZP7wyxbwRO2AhyzForm7494C6bMbY_HAeNHV6-WbSnc"
)
WORKSHEET_ID = int(os.environ.get("GOOGLE_WORKSHEET_ID", "451387449"))
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1504338147155251220")
TEMPLATE_CHANNEL_ID = os.environ.get(
    "DISCORD_TEMPLATE_CHANNEL_ID", "1504375083546972230"
)
TEMPLATE_THREAD_IDS = [
    tid.strip()
    for tid in os.environ.get("DISCORD_TEMPLATE_THREAD_IDS", "").split(",")
    if tid.strip()
]
LOG_CHANNEL_ID = os.environ.get("DISCORD_LOG_CHANNEL_ID", "1505927819094397038")

DATE_PLACEHOLDER = "yymmdd"

# 모듈 레벨 스케줄러 참조 (scheduler.py에서 주입)
_scheduler = None


def set_scheduler(scheduler):
    """외부 스케줄러 인스턴스를 주입받는다."""
    global _scheduler
    _scheduler = scheduler


def _fetch_templates() -> list[dict]:
    """템플릿 스레드들을 fetch하여 제목/본문을 가져온다."""
    templates = []
    for thread_id in TEMPLATE_THREAD_IDS:
        tmpl = fetch_thread_template(thread_id)
        templates.append(tmpl)
    return templates


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

    channels = get_guild_channels(GUILD_ID)
    forum_channels = {ch["name"]: ch for ch in channels if ch.get("type") == 15}

    channel = forum_channels.get(channel_name)
    if not channel:
        msg = f"[채널 없음] {channel_name}"
        print(f"  {msg}")
        if LOG_CHANNEL_ID:
            send_message(LOG_CHANNEL_ID, msg)
        return

    channel_id = channel["id"]
    templates = _fetch_templates()

    for tmpl in templates:
        new_title = tmpl["title"].replace(DATE_PLACEHOLDER, date_str)
        create_forum_thread(channel_id, title=new_title, content=tmpl["content"])
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

    schools = read_school_schedules(SPREADSHEET_ID, WORKSHEET_ID)
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

    schools = read_school_schedules(SPREADSHEET_ID, WORKSHEET_ID)
    print(f"  학교 수: {len(schools)}")

    # 해당 날짜에 수업이 있는 학교 찾기
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

    # 실제 실행 (--date로 수동 트리거)
    for school_name, end_time in notices:
        post_notice(school_name, today_str)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="수업 종료 후 Discord 공지 자동 게시")
    parser.add_argument(
        "--dry-run", action="store_true", help="실제 전송 없이 콘솔 출력만"
    )
    parser.add_argument("--date", type=str, help="대상 날짜 (YYYY-MM-DD 또는 MM-DD)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, target_date=args.date)
