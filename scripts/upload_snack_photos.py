"""
Discord 간식증빙사진을 구글 드라이브에 모아 올림

10분마다 실행되며:
1. "공지_팀모노리스"로 시작하는 카테고리의 "{학교}-공지" 포럼에서 제목에 "간식증빙"이 든 스레드를 찾음
2. 스레드의 이미지 첨부를 "{학교}_{M월D일}_AI동행프로그램.jpg" 이름으로 드라이브 폴더에 올림
   날짜는 스레드 제목의 수업 날짜다. 강사님이 다음 날 올려도 그 회차 사진이기 때문이다.
3. 메시지의 사진을 다 올리면 봇이 ✅ 반응을 달고, 다음부터 그 메시지는 건너뜀

한 번 올린 사진은 다시 올리면 안 된다. 드라이브는 사람이 파일을 지우거나 옮기므로
판단 근거가 될 수 없어 ✅ 를 기준으로 삼는다. 메시지 중간에 실패해 ✅ 가 없을 때를
위해 드라이브 파일 appProperties 의 Discord 첨부 ID 도 함께 확인한다.

평소에는 활성 스레드만 본다. 사진이 올라오면 스레드가 다시 활성이 되므로 충분하다.
보관된 스레드까지 훑는 것은 --backfill 로 한 번만 돌린다. 학교마다 스레드가 매주
쌓이므로 10분마다 전부 훑으면 Discord 요청이 수백 건이 된다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from api.discord import (
    create_reaction,
    get_active_threads,
    get_attachment,
    get_channel_messages,
    get_guild_channels,
    get_public_archived_threads,
)
from api.google_drive import create_file, list_folder_files

load_dotenv()

KST = timezone(timedelta(hours=9))

GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1504338147155251220")
# 운영기관마다 카테고리가 따로 있다. 다른 기관 학교의 사진은 우리 몫이 아니다.
# 학교가 늘면 "공지_팀모노리스2" 처럼 카테고리를 더 만들므로 접두어로 찾는다.
CATEGORY_PREFIX = "공지_팀모노리스"
FORUM_SUFFIX = "-공지"
THREAD_KEYWORD = "간식증빙"
PROGRAM_NAME = "AI동행프로그램"

CATEGORY_CHANNEL_TYPE = 4
FORUM_CHANNEL_TYPE = 15
MESSAGE_PAGE_SIZE = 100

ATTACHMENT_ID_KEY = "discord_attachment_id"
UPLOADED_EMOJI = "✅"
# 반응 추가는 채널당 0.25초에 한 번으로 제한된다. backfill 처럼 연달아 달면 429 가 난다.
REACTION_INTERVAL_SECONDS = 0.3
# 운영사업봇이 만드는 제목 "7.16(목) 12:40 - 🍪 간식증빙사진" 의 수업 날짜
CLASS_DATE_PATTERN = re.compile(r"^(\d{1,2})\.(\d{1,2})\(")


def find_school_forums(channels: list[dict]) -> dict[str, str]:
    """
    카테고리 아래의 "{학교}-공지" 포럼을 찾는다.

    Returns:
        dict[str, str]: 포럼 채널 ID → 학교명
    """
    category_ids = {
        ch["id"]
        for ch in channels
        if ch["type"] == CATEGORY_CHANNEL_TYPE
        and ch["name"].startswith(CATEGORY_PREFIX)
    }
    return {
        ch["id"]: ch["name"].removesuffix(FORUM_SUFFIX)
        for ch in channels
        if ch["type"] == FORUM_CHANNEL_TYPE
        and ch.get("parent_id") in category_ids
        and ch["name"].endswith(FORUM_SUFFIX)
    }


def list_archived_threads(forum_id: str) -> list[dict]:
    """포럼의 보관된 스레드를 끝까지 넘겨 가며 모은다."""
    threads: list[dict] = []
    before = None
    while True:
        payload = get_public_archived_threads(forum_id, before=before)
        threads.extend(payload["threads"])
        if not payload["has_more"]:
            return threads
        before = payload["threads"][-1]["thread_metadata"]["archive_timestamp"]


def list_snack_threads(forums: dict[str, str], backfill: bool) -> list[dict]:
    """학교 포럼의 간식증빙 스레드를 모은다."""
    threads = get_active_threads(GUILD_ID)["threads"]
    if backfill:
        for forum_id in forums:
            threads.extend(list_archived_threads(forum_id))
    return [
        t
        for t in threads
        if t.get("parent_id") in forums and THREAD_KEYWORD in t["name"]
    ]


def list_thread_messages(thread_id: str) -> list[dict]:
    """스레드의 메시지를 끝까지 넘겨 가며 모은다."""
    messages: list[dict] = []
    before = None
    while True:
        page = get_channel_messages(thread_id, limit=MESSAGE_PAGE_SIZE, before=before)
        messages.extend(page)
        if len(page) < MESSAGE_PAGE_SIZE:
            return messages
        before = page[-1]["id"]


def is_image(attachment: dict) -> bool:
    return attachment.get("content_type", "").startswith("image/")


def is_marked_uploaded(message: dict) -> bool:
    """봇이 단 ✅ 가 있으면 이미 올린 메시지다. 사람이 단 ✅ 는 세지 않는다."""
    return any(
        r["emoji"]["name"] == UPLOADED_EMOJI and r["me"]
        for r in message.get("reactions", [])
    )


def class_date_label(thread_name: str, posted_at: datetime) -> str:
    """
    스레드 제목에서 수업 날짜를 "M월D일" 로 꺼낸다.

    운영사업봇 이전에 사람이 만든 스레드는 제목에 날짜가 없을 수 있다. 그때는
    "수업 당일까지 업로드" 규칙에 기대어 업로드 날짜를 쓴다.
    """
    match = CLASS_DATE_PATTERN.match(thread_name)
    if not match:
        return f"{posted_at.month}월{posted_at.day}일"
    return f"{int(match[1])}월{int(match[2])}일"


def make_file_name(school: str, date_label: str, ext: str, taken: set[str]) -> str:
    """
    "{학교}_{M월D일}_AI동행프로그램{ext}" 를 만든다.
    같은 날 사진이 여러 장이면 뒤에 _2, _3 을 붙인다.
    """
    base = f"{school}_{date_label}_{PROGRAM_NAME}"
    name = f"{base}{ext}"
    n = 2
    while name in taken:
        name = f"{base}_{n}{ext}"
        n += 1
    return name


def upload_thread(
    thread: dict,
    school: str,
    folder_id: str,
    uploaded_ids: set[str],
    taken_names: set[str],
    dry_run: bool,
) -> int:
    """스레드 하나의 새 사진을 올리고 올린 장수를 돌려준다."""
    count = 0
    # 오래된 메시지부터 올려야 _2, _3 번호가 올린 순서와 맞는다.
    for message in reversed(list_thread_messages(thread["id"])):
        images = [a for a in message["attachments"] if is_image(a)]
        if not images or is_marked_uploaded(message):
            continue
        posted_at = datetime.fromisoformat(message["timestamp"]).astimezone(KST)
        date_label = class_date_label(thread["name"], posted_at)
        for attachment in images:
            if attachment["id"] in uploaded_ids:
                continue
            ext = Path(attachment["filename"]).suffix.lower()
            name = make_file_name(school, date_label, ext, taken_names)
            taken_names.add(name)
            if dry_run:
                print(f"  [DRY-RUN] {name}")
                continue

            content = get_attachment(attachment["url"])
            create_file(
                folder_id,
                name,
                content,
                attachment["content_type"],
                {ATTACHMENT_ID_KEY: attachment["id"]},
            )
            uploaded_ids.add(attachment["id"])
            count += 1
            print(f"  업로드: {name}")

        if not dry_run:
            create_reaction(thread["id"], message["id"], UPLOADED_EMOJI)
            time.sleep(REACTION_INTERVAL_SECONDS)

    return count


def main(dry_run: bool = False, backfill: bool = False):
    """
    간식증빙 스레드의 새 사진을 드라이브에 올린다.

    Args:
        dry_run: 업로드 없이 대상만 출력
        backfill: 보관된 스레드까지 훑는다 (최초 1회용)
    """
    folder_id = os.environ["DISCORD_SNACK_PHOTO_DRIVE_FOLDER_ID"]
    print(f"[upload_snack_photos] 실행: {datetime.now(KST).isoformat()}")

    forums = find_school_forums(get_guild_channels(GUILD_ID))
    threads = list_snack_threads(forums, backfill)
    print(f"  학교 포럼 {len(forums)}개, 간식증빙 스레드 {len(threads)}개")

    existing = list_folder_files(folder_id)
    uploaded_ids = {
        f["appProperties"][ATTACHMENT_ID_KEY]
        for f in existing
        if ATTACHMENT_ID_KEY in f.get("appProperties", {})
    }
    taken_names = {f["name"] for f in existing}

    count = 0
    failures = []
    # 한 학교의 실패(권한 없는 포럼, 일시적 오류)가 나머지 학교를 막지 않게 격리한다.
    # 코드 버그는 잡지 않고, 잡은 실패도 마지막에 터뜨려 스케줄러 로그에 남긴다.
    for thread in threads:
        school = forums[thread["parent_id"]]
        try:
            count += upload_thread(
                thread, school, folder_id, uploaded_ids, taken_names, dry_run
            )
        except requests.RequestException as e:
            failures.append(f"{school} {thread['name']}: {type(e).__name__}: {e}")
            print(f"  [실패] {failures[-1]}")

    print(f"  새로 올린 사진 {count}장")
    if failures:
        raise RuntimeError(f"간식증빙 업로드 실패 {len(failures)}건: {failures}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discord 간식증빙사진 드라이브 업로드")
    parser.add_argument(
        "--dry-run", action="store_true", help="업로드 없이 대상만 출력"
    )
    parser.add_argument(
        "--backfill", action="store_true", help="보관된 스레드까지 훑기 (최초 1회)"
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, backfill=args.backfill)
