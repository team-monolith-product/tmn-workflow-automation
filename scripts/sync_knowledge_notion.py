"""
노션 통합의 콘텐츠 사용 권한 전체를 지식베이스에 동기화합니다.

수집 범위는 노션 연결 설정에서 통합에 부여한 권한 그대로입니다. 등록 목록을
따로 두지 않습니다. 두 곳을 맞춰야 하는 상태를 만들면 언젠가 어긋나고, 어느
쪽이 맞는지 알 수 없게 됩니다.

권한에서 빠진 것은 지웁니다. 권한을 내렸는데 검색에는 계속 나오면 내린 것이
아닙니다. 최상위 항목이 빠지면 data_source를 지우고 딸린 item이 함께 사라지며,
페이지 하나만 빠지면 그 item만 지웁니다.

data_source는 최상위 항목마다 하나입니다. 부모를 따라 올라가다 권한 밖으로
나가는 자리가 최상위이고, 그게 연결 설정 화면에 보이는 목록입니다. 검색 결과에
"Product HQ" 같은 출처가 찍히는 것도 이 단위입니다.

두 번째 실행부터는 마지막 동기화 이후 편집된 페이지만 본문을 받습니다. 목록을
훑는 search는 매번 하지만, 블록을 전부 읽어 마크다운으로 바꾸는 비싼 일은
건너뜁니다.

사용법:
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
from app.knowledge import DISTILL_DELAY_SECONDS
from service.knowledge.db import connect, fetch_all
from service.knowledge.ingest import upsert_item
from service.knowledge.notion import (
    build_page_row,
    derive_roots,
    fetch_accessible,
    fetch_user_emails,
    node_title,
)
from service.knowledge.register import upsert_source

NOTION_SOURCES = """
SELECT id, external_id, name, last_synced_at
FROM data_source
WHERE source = 'notion'
"""

# 권한에서 빠진 최상위 항목. item은 ON DELETE CASCADE로 함께 사라진다.
DELETE_GONE_SOURCES = """
DELETE FROM data_source
WHERE source = 'notion' AND NOT (external_id = ANY(%(root_ids)s))
"""

# 최상위는 남았는데 페이지만 빠진 경우.
DELETE_GONE_ITEMS = """
DELETE FROM item
USING data_source
WHERE item.data_source_id = data_source.id
  AND data_source.source = 'notion'
  AND NOT (item.external_id = ANY(%(page_ids)s))
"""

MARK_SYNCED = "UPDATE data_source SET last_synced_at = now() WHERE id = %s"


def sync(dsn: str | None, full: bool, dry_run: bool) -> None:
    """통합 권한 전체를 동기화합니다.

    Args:
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL
        full: True면 last_synced_at을 무시하고 전부 다시 받습니다
        dry_run: True면 아무것도 쓰지 않고 대상만 셉니다
    """
    client = Client(auth=os.environ["NOTION_TOKEN"])
    user_emails = fetch_user_emails(client)

    nodes = fetch_accessible(client)
    roots = derive_roots(nodes)
    by_id = {node["id"]: node for node in nodes}
    pages = [node for node in nodes if node["object"] == "page"]

    root_ids = sorted(set(roots.values()))
    print(f"노션 사용자 {len(user_emails)}명")
    print(f"권한 안에 페이지 {len(pages)}개, 최상위 항목 {len(root_ids)}개\n")

    with connect(dsn) as conn:
        known = {row["external_id"]: row for row in fetch_all(conn, NOTION_SOURCES)}
        source_ids = _sync_sources(conn, by_id, root_ids, known, dry_run)
        _prune(conn, root_ids, [page["id"] for page in pages], known, dry_run)

        counts: dict[str, list[int]] = {root_id: [0, 0] for root_id in root_ids}
        for page in pages:
            root_id = roots[page["id"]]
            counts[root_id][0] += 1

            synced_at = (
                None if full else (known.get(root_id) or {}).get("last_synced_at")
            )
            if synced_at is not None:
                if datetime.fromisoformat(page["last_edited_time"]) <= synced_at:
                    continue
            counts[root_id][1] += 1

            if dry_run:
                continue

            row = build_page_row(
                data_source_id=source_ids[root_id],
                page=page,
                markdown=notion_page_to_markdown(page["id"]),
                distill_delay_seconds=DISTILL_DELAY_SECONDS,
                user_emails=user_emails,
            )
            upsert_item(conn, row)
            conn.commit()

        for root_id in root_ids:
            walked, changed = counts[root_id]
            name = node_title(by_id[root_id])
            print(f"{name:<30} 페이지 {walked:>5} 적재 {changed:>5}")
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(MARK_SYNCED, (source_ids[root_id],))
                conn.commit()


def _sync_sources(
    conn: psycopg.Connection,
    by_id: dict[str, dict],
    root_ids: list[str],
    known: dict[str, dict],
    dry_run: bool,
) -> dict[str, int]:
    """최상위 항목을 data_source로 맞춥니다.

    Args:
        conn: 커넥션
        by_id: 노드 ID → 노드
        root_ids: 최상위 항목 ID
        known: 이미 등록된 노션 data_source
        dry_run: True면 쓰지 않습니다

    Returns:
        dict[str, int]: 최상위 항목 ID → data_source.id. dry_run이면 빈 값
    """
    added = [root_id for root_id in root_ids if root_id not in known]
    if added:
        print(f"권한에 새로 들어옴: {', '.join(node_title(by_id[i]) for i in added)}")

    if dry_run:
        return {}

    source_ids = {
        root_id: upsert_source(
            conn, "notion", root_id, node_title(by_id[root_id]), {"kind": "root"}
        )
        for root_id in root_ids
    }
    conn.commit()
    return source_ids


def _prune(
    conn: psycopg.Connection,
    root_ids: list[str],
    page_ids: list[str],
    known: dict[str, dict],
    dry_run: bool,
) -> None:
    """권한에서 빠진 것을 지웁니다.

    Args:
        conn: 커넥션
        root_ids: 지금 권한 안에 있는 최상위 항목 ID
        page_ids: 지금 권한 안에 있는 페이지 ID
        known: 이미 등록된 노션 data_source
        dry_run: True면 지우지 않고 알리기만 합니다
    """
    gone = [
        name
        for ext, row in known.items()
        if ext not in root_ids
        for name in [row["name"]]
    ]
    if gone:
        print(f"권한에서 빠져 지움: {', '.join(gone)}")

    if dry_run:
        return

    with conn.cursor() as cur:
        cur.execute(DELETE_GONE_SOURCES, {"root_ids": root_ids})
        dropped_sources = cur.rowcount
        cur.execute(DELETE_GONE_ITEMS, {"page_ids": page_ids})
        dropped_items = cur.rowcount
    conn.commit()

    if dropped_sources or dropped_items:
        print(f"삭제: 최상위 {dropped_sources}, 페이지 {dropped_items}\n")


def main() -> None:
    """명령행 인자를 파싱해 동기화를 실행합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="노션 지식베이스 동기화")
    parser.add_argument("--dsn", help="접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL")
    parser.add_argument(
        "--full", action="store_true", help="last_synced_at 무시하고 전부 다시 받음"
    )
    parser.add_argument("--dry-run", action="store_true", help="쓰지 않고 대상만 셈")
    args = parser.parse_args()

    sync(args.dsn, args.full, args.dry_run)


if __name__ == "__main__":
    main()
