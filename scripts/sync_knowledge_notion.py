"""
등록한 노션 루트 페이지 아래를 지식베이스에 동기화합니다.

수집 범위는 data_source에 등록한 루트 페이지와 그 하위 페이지 전부입니다.
통합에 공유된 것을 search로 훑지 않습니다. 공유 설정을 바꾼 사람이 모르는
사이에 수집 범위가 바뀌면 안 됩니다.

노션 웹훅으로 증분을 받는 것은 나중입니다. 웹훅부터 붙이면 과거 페이지가
영영 들어오지 않으므로 전량 동기화가 먼저입니다. 웹훅이 붙어도 이벤트에는
본문이 없어 페이지를 다시 읽어야 하는데, 그때도 여기 있는 build_page_row를
그대로 지나갑니다. 슬랙에서 Export 백필과 Socket Mode 증분이 같은 함수를
쓰는 것과 같습니다.

두 번째 실행부터는 last_synced_at 이후에 편집된 페이지만 본문을 받아옵니다.
페이지 목록을 훑는 것은 매번 하지만, 블록을 전부 읽어 마크다운으로 바꾸는
비싼 일은 건너뜁니다.

사용법:
    python scripts/sync_knowledge_notion.py --register https://www.notion.so/... --name 개발팀
    python scripts/sync_knowledge_notion.py --dry-run
    python scripts/sync_knowledge_notion.py --full
    python scripts/sync_knowledge_notion.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from datetime import datetime

import psycopg
from dotenv import load_dotenv
from notion_client import Client

from app.common import notion_page_to_markdown
from app.justin import extract_notion_page_id
from app.knowledge import DISTILL_DELAY_SECONDS
from service.knowledge.db import connect, fetch_all
from service.knowledge.ingest import upsert_item
from service.knowledge.notion import (
    build_page_row,
    fetch_user_emails,
    page_title,
    walk_page_ids,
)
from service.knowledge.register import upsert_source

ENABLED_SOURCES = """
SELECT id, external_id, name, last_synced_at
FROM data_source
WHERE source = 'notion' AND enabled
ORDER BY name
"""

MARK_SYNCED = "UPDATE data_source SET last_synced_at = now() WHERE id = %s"


def register_root(client: Client, url: str, name: str | None, dsn: str | None) -> None:
    """루트 페이지를 수집 대상으로 등록합니다.

    Args:
        client: 노션 클라이언트
        url: 노션 페이지 URL 또는 32자 hex ID
        name: 사람이 읽는 이름. 생략하면 페이지 제목을 씁니다
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL
    """
    page_id = extract_notion_page_id(url) or url
    page = client.pages.retrieve(page_id)

    with connect(dsn) as conn:
        data_source_id = upsert_source(
            conn, "notion", page["id"], name or page_title(page), {"kind": "page"}
        )
        conn.commit()
    print(f"등록 완료 #{data_source_id} {name or page_title(page)} ({page['url']})")


def sync_root(
    conn: psycopg.Connection,
    client: Client,
    source: dict,
    user_emails: dict[str, str],
    since: datetime | None,
    dry_run: bool,
) -> tuple[int, int, int]:
    """루트 하나를 훑어 적재합니다.

    Args:
        conn: 커넥션
        client: 노션 클라이언트
        source: data_source 행
        user_emails: 노션 사용자 ID → 이메일
        since: 이 시각 이후에 편집된 페이지만 본문을 받습니다. None이면 전부
        dry_run: True면 적재하지 않고 셉니다

    Returns:
        tuple: (훑은 페이지 수, 적재한 수, 신규 수)
    """
    walked = fetched = inserted = 0
    for page_id in walk_page_ids(client, source["external_id"]):
        walked += 1
        page = client.pages.retrieve(page_id)

        last_edited = datetime.fromisoformat(page["last_edited_time"])
        if since is not None and last_edited <= since:
            continue

        fetched += 1
        if dry_run:
            continue

        row = build_page_row(
            data_source_id=source["id"],
            root_id=source["external_id"],
            page=page,
            markdown=notion_page_to_markdown(page_id),
            distill_delay_seconds=DISTILL_DELAY_SECONDS,
            user_emails=user_emails,
        )
        inserted += upsert_item(conn, row)["inserted"]

    return walked, fetched, inserted


def sync(dsn: str | None, full: bool, dry_run: bool) -> None:
    """등록된 노션 루트를 전부 동기화합니다.

    루트 하나가 끝날 때마다 커밋합니다. 중간에 끊겨도 끝난 루트는 남습니다.

    Args:
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL
        full: True면 last_synced_at을 무시하고 전부 다시 받습니다
        dry_run: True면 적재하지 않고 대상만 셉니다
    """
    client = Client(auth=os.environ["NOTION_TOKEN"])
    user_emails = fetch_user_emails(client)
    print(f"노션 사용자 {len(user_emails)}명")

    with connect(dsn) as conn:
        sources = fetch_all(conn, ENABLED_SOURCES)
        print(f"등록된 루트 {len(sources)}개\n")

        for source in sources:
            since = None if full else source["last_synced_at"]
            walked, fetched, inserted = sync_root(
                conn, client, source, user_emails, since, dry_run
            )

            if dry_run:
                print(f"{source['name']:<30} 페이지 {walked:>5} 변경 {fetched:>5}")
                continue

            with conn.cursor() as cur:
                cur.execute(MARK_SYNCED, (source["id"],))
            conn.commit()
            print(
                f"{source['name']:<30} 페이지 {walked:>5}"
                f" 적재 {fetched:>5} 신규 {inserted:>5} 갱신 {fetched - inserted:>5}"
            )


def main() -> None:
    """명령행 인자를 파싱해 등록 또는 동기화를 실행합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="노션 지식베이스 동기화")
    parser.add_argument("--register", help="이 노션 페이지를 루트로 등록하고 끝냅니다")
    parser.add_argument("--name", help="--register와 함께. 생략하면 페이지 제목")
    parser.add_argument("--dsn", help="접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL")
    parser.add_argument(
        "--full", action="store_true", help="last_synced_at 무시하고 전부 다시 받음"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="적재하지 않고 대상만 셈"
    )
    args = parser.parse_args()

    if args.register:
        client = Client(auth=os.environ["NOTION_TOKEN"])
        register_root(client, args.register, args.name, args.dsn)
        return

    sync(args.dsn, args.full, args.dry_run)


if __name__ == "__main__":
    main()
