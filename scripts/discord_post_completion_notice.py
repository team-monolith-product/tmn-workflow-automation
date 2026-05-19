"""
수업 종료 후 Discord 포럼 채널에 자동 공지 게시

10분마다 실행되며:
1. Google Sheets에서 학교별 수업 일정을 읽음
2. (now - 15분, now] 윈도우에 종료시각이 들어가는 학교를 감지 (10분 주기 + 5분 여유)
3. "{학교이름}-공지" 포럼 채널에 템플릿 기반 게시물 3개 작성
4. 멱등성: 같은 제목의 활성 스레드가 이미 있으면 스킵
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from datetime import datetime, timedelta, timezone

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

DATE_PLACEHOLDER = "yymmdd"
FORUM_CHANNEL_TYPE = 15
SCHOOL_NAME_COL = 0
WINDOW_MINUTES = 15


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

            if date_val == "":
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
    rows = get_worksheet_values(SPREADSHEET_ID, WORKSHEET_ID)
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


def make_title(template_title: str, end_time: datetime) -> str:
    """템플릿 제목에서 'yymmdd' 부분을 'yymmdd_hhmm'으로 치환."""
    marker = end_time.strftime("%y%m%d_%H%M")
    return template_title.replace(DATE_PLACEHOLDER, marker)


def main(dry_run: bool = False, target_date: str | None = None):
    """
    10분마다 호출되어 윈도우 안의 종료시각을 가진 학교에 공지를 게시한다.

    Args:
        dry_run: 실제 Discord 전송 없이 콘솔 출력만
        target_date: 테스트용 날짜 지정 (YYYY-MM-DD or MM-DD). 지정 시 하루 전체 윈도우.
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
        window_start = datetime(year, month, day, 0, 0, tzinfo=KST)
    else:
        window_start = now - timedelta(minutes=WINDOW_MINUTES)

    print(f"[discord_post_completion_notice] 실행: {now.isoformat()}")
    print(f"  윈도우: ({window_start.strftime('%H:%M')}, {now.strftime('%H:%M')}]")

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
            marker = end_time.strftime("%y%m%d_%H%M")
            print(f"  [DRY-RUN] {channel_name}")
            print(f"    → {marker}_간식증빙사진")
            print(f"    → {marker}_출석부사진")
            print(f"    → {marker}_수업사진")
        return

    # 템플릿/채널/활성 스레드 한 번씩 조회
    templates = fetch_templates()
    channels = get_guild_channels(GUILD_ID)
    active_threads = get_active_threads(GUILD_ID).get("threads", [])

    for school_name, end_time in targets:
        channel_name = f"{school_name}-공지"
        channel = find_forum_channel(channels, channel_name)
        if not channel:
            msg = f"[채널 없음] {channel_name}"
            print(f"  {msg}")
            if LOG_CHANNEL_ID:
                create_message(LOG_CHANNEL_ID, msg)
            continue

        channel_id = channel["id"]
        existing_titles = {
            t.get("name", "")
            for t in active_threads
            if t.get("parent_id") == channel_id
        }

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
