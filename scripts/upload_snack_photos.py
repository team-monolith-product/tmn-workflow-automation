"""
Discord 간식증빙사진을 구글 드라이브에 모아 올림

10분마다 실행되며:
1. "공지_팀모노리스" 카테고리의 "{학교}-공지" 포럼에서 제목에 "간식증빙"이 든 스레드를 찾음
2. 스레드의 이미지 첨부를 내려받아 LLM 으로 간식 종류를 판별
3. "{학교}_{M월D일}_{간식}_AI동행프로그램.jpg" 이름으로 드라이브 폴더에 올림
4. 멱등성: 드라이브 파일의 appProperties 에 Discord 첨부 ID 를 남겨, 이미 올린 첨부는 스킵

평소에는 활성 스레드만 본다. 사진이 올라오면 스레드가 다시 활성이 되므로 충분하다.
보관된 스레드까지 훑는 것은 --backfill 로 한 번만 돌린다. 학교마다 스레드가 매주
쌓이므로 10분마다 전부 훑으면 Discord 요청이 수백 건이 된다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import base64
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

from api.discord import (
    get_active_threads,
    get_attachment,
    get_channel_messages,
    get_guild_channels,
    get_public_archived_threads,
)
from api.google_drive import create_file, list_folder_files
from service.llm import DEFAULT_MODEL

load_dotenv()

KST = timezone(timedelta(hours=9))

GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1504338147155251220")
# 운영기관마다 카테고리가 따로 있다. 다른 기관 학교의 사진은 우리 몫이 아니다.
CATEGORY_NAME = "공지_팀모노리스"
FORUM_SUFFIX = "-공지"
THREAD_KEYWORD = "간식증빙"
PROGRAM_NAME = "AI동행프로그램"
# 사진에서 간식을 못 알아봤을 때 파일명에 넣을 이름
FALLBACK_SNACK = "간식"

CATEGORY_CHANNEL_TYPE = 4
FORUM_CHANNEL_TYPE = 15
MESSAGE_PAGE_SIZE = 100

ATTACHMENT_ID_KEY = "discord_attachment_id"

SNACK_PROMPT = f"""학생들에게 나눠 준 간식 사진입니다. 사진 속 간식 종류를 한국어 명사 하나로만 답하세요.
- 예: 햄버거, 피자, 샌드위치, 도넛, 컵라면
- 여러 종류면 가장 많은 것 하나만
- 간식이 보이지 않으면 "{FALLBACK_SNACK}"
- 설명, 문장부호, 따옴표 없이 단어만"""


def find_school_forums(channels: list[dict]) -> dict[str, str]:
    """
    카테고리 아래의 "{학교}-공지" 포럼을 찾는다.

    Returns:
        dict[str, str]: 포럼 채널 ID → 학교명
    """
    category_ids = {
        ch["id"]
        for ch in channels
        if ch["type"] == CATEGORY_CHANNEL_TYPE and ch["name"] == CATEGORY_NAME
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


def clean_snack_name(text: str) -> str:
    """LLM 답에서 파일명에 쓸 간식 이름만 남긴다. 구분자인 _ 와 경로 문자는 뺀다."""
    word = text.strip().split()[0] if text.strip() else ""
    word = word.strip("\"'.,").replace("_", "").replace("/", "").replace("\\", "")
    return word or FALLBACK_SNACK


def ask_snack_name(client: OpenAI, content: bytes, mime_type: str) -> str:
    """사진을 보고 간식 종류를 판별한다."""
    data_url = f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
    response = client.chat.completions.create(
        model=DEFAULT_MODEL,
        reasoning_effort="low",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SNACK_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    return clean_snack_name(response.choices[0].message.content or "")


def make_file_name(
    school: str, posted_at: datetime, snack: str, ext: str, taken: set[str]
) -> str:
    """
    "{학교}_{M월D일}_{간식}_AI동행프로그램{ext}" 를 만든다.
    같은 날 같은 간식 사진이 여러 장이면 뒤에 _2, _3 을 붙인다.
    """
    base = f"{school}_{posted_at.month}월{posted_at.day}일_{snack}_{PROGRAM_NAME}"
    name = f"{base}{ext}"
    n = 2
    while name in taken:
        name = f"{base}_{n}{ext}"
        n += 1
    return name


def main(dry_run: bool = False, backfill: bool = False):
    """
    간식증빙 스레드의 새 사진을 드라이브에 올린다.

    Args:
        dry_run: LLM 판별과 업로드 없이 대상만 출력
        backfill: 보관된 스레드까지 훑는다 (최초 1회용)
    """
    folder_id = os.environ["SNACK_PHOTO_DRIVE_FOLDER_ID"]
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

    client = OpenAI()
    count = 0
    for thread in threads:
        school = forums[thread["parent_id"]]
        # 오래된 메시지부터 올려야 _2, _3 번호가 올린 순서와 맞는다.
        for message in reversed(list_thread_messages(thread["id"])):
            posted_at = datetime.fromisoformat(message["timestamp"]).astimezone(KST)
            for attachment in message["attachments"]:
                if not is_image(attachment) or attachment["id"] in uploaded_ids:
                    continue
                ext = Path(attachment["filename"]).suffix.lower()
                if dry_run:
                    print(
                        f"  [DRY-RUN] {school} {posted_at:%m-%d} {attachment['filename']}"
                    )
                    continue

                content = get_attachment(attachment["url"])
                snack = ask_snack_name(client, content, attachment["content_type"])
                name = make_file_name(school, posted_at, snack, ext, taken_names)
                create_file(
                    folder_id,
                    name,
                    content,
                    attachment["content_type"],
                    {ATTACHMENT_ID_KEY: attachment["id"]},
                )
                taken_names.add(name)
                uploaded_ids.add(attachment["id"])
                count += 1
                print(f"  업로드: {name}")

    print(f"  새로 올린 사진 {count}장")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discord 간식증빙사진 드라이브 업로드")
    parser.add_argument(
        "--dry-run", action="store_true", help="LLM 판별·업로드 없이 대상만 출력"
    )
    parser.add_argument(
        "--backfill", action="store_true", help="보관된 스레드까지 훑기 (최초 1회)"
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, backfill=args.backfill)
