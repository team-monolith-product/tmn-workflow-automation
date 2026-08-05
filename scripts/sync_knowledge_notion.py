"""
노션 통합의 콘텐츠 사용 권한 전체를 지식베이스에 동기화합니다.

수집 범위는 노션 연결 설정에서 통합에 부여한 권한 그대로입니다. 등록 목록을
따로 두지 않습니다. 두 곳을 맞춰야 하는 상태를 만들면 언젠가 어긋나고, 어느
쪽이 맞는지 알 수 없게 됩니다.

권한에서 빠진 것은 지웁니다. 권한을 내렸는데 검색에 계속 나오면 내린 것이
아닙니다.

## 데이터베이스 행

권한 안 페이지 8.5만 개 중 8.3만 개가 데이터베이스 행이고, 대부분은 문서가
아니라 표입니다. 구매 내역, 결산 자료, 학교 목록 같은 것들입니다. 반대로 행
자체가 문서인 데이터베이스도 있습니다. 회의록, 일반 문서, 출장보고서입니다.

본문 길이로 가릅니다(MIN_BODY_CHARS). 이름을 코드에 적어두면 데이터베이스가
늘 때마다 코드를 고쳐야 합니다.

길이를 알려면 페이지마다 블록을 받아야 하는데 8.3만 번은 13시간입니다. 그래서
같은 기준을 두 단계로 겁니다. 데이터베이스마다 표본 몇 행을 재서 본문이 없는
표를 통째로 걸러내고, 남은 것에만 행별로 겁니다. 표본은 데이터베이스 수에만
비례하므로 수백 배 싸고, 판정 기준은 같습니다.

## data_source 단위

워크스페이스 직속 페이지, 곧 연결 설정 화면의 팀스페이스가 data_source
하나입니다. 부여한 권한 단위와 검색 결과의 출처가 일치해야 이해하기 쉽습니다.
데이터베이스 이름을 쓰지 않는 이유는 "스터디"나 "가이드 문서"처럼 겹치는 것이
많아 출처로 모호하기 때문입니다.

거기까지 올라가려면 사슬을 두 군데서 이어야 합니다. search가 page와
data_source만 돌려주므로 데이터베이스와 블록은 따로 받아옵니다.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import psycopg
from dotenv import load_dotenv
from notion_client import Client

from app.common import notion_page_to_markdown
from app.knowledge import DISTILL_DELAY_SECONDS
from service.knowledge.db import connect, fetch_all
from service.knowledge.ingest import upsert_item
from service.knowledge.notion import (
    MIN_BODY_CHARS,
    build_page_row,
    derive_roots,
    fetch_accessible,
    fetch_user_emails,
    has_document_body,
    is_database_row,
    node_title,
    parent_ref,
    sample_evenly,
)
from service.knowledge.register import upsert_source

# 데이터베이스 하나를 판정할 표본 행 수.
SAMPLE_SIZE = 8

# 본문 받기는 네트워크 대기가 대부분이라 동시에 돌린다. 노션 rate limit이
# 평균 초당 3회라 그 위로는 올려도 429만 더 받는다.
CONCURRENCY = 3

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

# 최상위는 남았는데 페이지만 빠진 경우. 본문이 짧아져 기준에서 밀려난 행도
# 여기로 온다. 인자는 "색인에 남아 있어야 할 페이지" 전체이지 이번 회차에
# 적재한 것이 아니다. 후자를 넘기면 안 바뀐 페이지가 매번 지워진다.
DELETE_GONE_ITEMS = """
DELETE FROM item
USING data_source
WHERE item.data_source_id = data_source.id
  AND data_source.source = 'notion'
  AND NOT (item.external_id = ANY(%(page_ids)s))
"""

MARK_SYNCED = "UPDATE data_source SET last_synced_at = now() WHERE id = %s"


def body_of(page_id: str) -> str:
    """페이지 본문을 마크다운으로 받아옵니다. 실패하면 빈 문자열입니다.

    한 페이지가 실패해도 8만 건짜리 훑기를 처음부터 다시 하지 않기 위해서만
    삼킵니다. 실패한 행은 본문이 없는 것으로 판정되어 이번 회차에 빠지고,
    다음 회차에 다시 시도합니다.

    Args:
        page_id: 노션 페이지 ID

    Returns:
        str: 마크다운 본문
    """
    try:
        return notion_page_to_markdown(page_id) or ""
    except Exception as exc:
        print(f"  본문 실패 {page_id}: {type(exc).__name__}")
        return ""


def classify_databases(
    rows_by_database: dict[str, list[dict]], titles: dict[str, str]
) -> set[str]:
    """표본으로 문서형 데이터베이스를 가려냅니다.

    Args:
        rows_by_database: 데이터소스 ID → 그 안의 행
        titles: 데이터소스 ID → 이름

    Returns:
        set[str]: 문서형으로 판정된 데이터소스 ID
    """
    samples = {
        database_id: sample_evenly(rows, SAMPLE_SIZE)
        for database_id, rows in rows_by_database.items()
    }
    flat = [(d, row) for d, rows in samples.items() for row in rows]
    print(f"데이터베이스 {len(samples)}개를 표본 {len(flat)}행으로 판정하는 중")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        lengths = list(executor.map(lambda item: len(body_of(item[1]["id"])), flat))

    measured: dict[str, list[int]] = {d: [] for d in samples}
    for (database_id, _), length in zip(flat, lengths):
        measured[database_id].append(length)

    documental = {d for d, lens in measured.items() if has_document_body(lens)}
    print(
        f"문서형 {len(documental)}개, 표로 판정해 제외 {len(samples) - len(documental)}개"
    )
    for database_id in sorted(documental, key=lambda d: -len(rows_by_database[d])):
        print(
            f"  {titles.get(database_id, '?')[:38]:<40} {len(rows_by_database[database_id]):>6}행"
        )
    return documental


def sync(dsn: str | None, full: bool, dry_run: bool) -> None:
    """통합 권한 전체를 동기화합니다.

    Args:
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL
        full: True면 last_synced_at을 무시하고 전부 다시 받습니다
        dry_run: True면 아무것도 쓰지 않고 대상만 셉니다
    """
    client = Client(auth=os.environ["NOTION_TOKEN"].strip())
    user_emails = fetch_user_emails(client)

    nodes = fetch_accessible(client)
    by_id = {node["id"]: node for node in nodes}
    parents: dict[str, tuple[str, str] | None] = {}

    # search가 돌려주지 않는 조상을 종류에 맞는 API로 받아온다. 데이터소스가
    # 여기 있는 이유는 search가 데이터소스를 다 돌려주지 않아서다. 실측에서
    # 행이 참조하는 470개 중 28개가 빠져 있었고 그 아래 5,182행이 딸려 있었다.
    RETRIEVE = {
        "database_id": lambda i: client.databases.retrieve(i),
        "data_source_id": lambda i: client.data_sources.retrieve(i),
        "block_id": lambda i: client.blocks.retrieve(i),
        "page_id": lambda i: client.pages.retrieve(i),
    }

    def fetch_parent(kind: str, node_id: str) -> tuple[str, str] | None:
        """search에 나오지 않는 조상의 부모를 받아옵니다."""
        if node_id not in parents:
            retrieve = RETRIEVE.get(kind)
            try:
                parents[node_id] = parent_ref(retrieve(node_id)) if retrieve else None
            except Exception:
                parents[node_id] = None
        return parents[node_id]

    roots = derive_roots(nodes, fetch_parent=fetch_parent)
    pages = [node for node in nodes if node["object"] == "page"]
    documents = [page for page in pages if not is_database_row(page)]

    # 판정 단위는 데이터베이스이므로 최상위가 아니라 행의 부모로 묶는다.
    # 최상위는 팀스페이스라 그것으로 묶으면 성격이 다른 데이터베이스가 한
    # 표본에 섞인다.
    rows_by_database: dict[str, list[dict]] = {}
    for page in pages:
        if is_database_row(page):
            rows_by_database.setdefault(parent_ref(page)[1], []).append(page)

    print(f"노션 사용자 {len(user_emails)}명")
    print(
        f"문서 페이지 {len(documents)}개, 데이터베이스 행 {len(pages) - len(documents)}개\n"
    )

    titles = {node["id"]: node_title(node) for node in nodes}
    documental = classify_databases(rows_by_database, titles)

    candidates = list(documents)
    for database_id in documental:
        candidates.extend(rows_by_database[database_id])

    # 최상위를 못 찾은 페이지는 넣지 않는다. 사슬이 중간에 끊기면 최상위가
    # 블록이나 지워진 데이터베이스의 ID가 되는데, 그것으로 data_source를
    # 만들면 이름도 실체도 없는 출처가 검색 결과에 찍힌다.
    targets = [page for page in candidates if roots[page["id"]] in by_id]
    dropped = len(candidates) - len(targets)
    if dropped:
        print(f"최상위를 못 찾아 제외 {dropped}개")

    root_ids = sorted({roots[page["id"]] for page in targets})
    print(f"\n적재 후보 {len(targets)}개, 최상위 {len(root_ids)}개\n")

    with connect(dsn) as conn:
        known = {row["external_id"]: row for row in fetch_all(conn, NOTION_SOURCES)}
        source_ids = _sync_sources(conn, titles, root_ids, dry_run)

        # 색인에 남아 있어야 할 페이지. 이번 회차에 건드리지 않은 것도 포함한다.
        # 여기 없는 노션 item은 뒤에서 지운다.
        retained = {page["id"] for page in targets}

        stored = 0
        for page in targets:
            root_id = roots[page["id"]]
            synced_at = (
                None if full else (known.get(root_id) or {}).get("last_synced_at")
            )
            if synced_at is not None:
                if datetime.fromisoformat(page["last_edited_time"]) <= synced_at:
                    continue

            if dry_run:
                stored += 1
                continue

            markdown = body_of(page["id"])
            # 데이터베이스 행은 여기서 한 번 더 거른다. 표본 판정이 느슨해
            # 문서형으로 들어온 데이터베이스에도 빈 행이 섞여 있다.
            if is_database_row(page) and len(markdown) < MIN_BODY_CHARS:
                retained.discard(page["id"])
                continue

            upsert_item(
                conn,
                build_page_row(
                    data_source_id=source_ids[root_id],
                    page=page,
                    markdown=markdown,
                    distill_delay_seconds=DISTILL_DELAY_SECONDS,
                    user_emails=user_emails,
                ),
            )
            conn.commit()
            stored += 1
            if stored % 100 == 0:
                print(f"  적재 {stored}")

        print(f"\n적재 {stored}건")
        if dry_run:
            return

        _prune(conn, root_ids, sorted(retained), known)
        for root_id in root_ids:
            with conn.cursor() as cur:
                cur.execute(MARK_SYNCED, (source_ids[root_id],))
        conn.commit()


def _sync_sources(
    conn: psycopg.Connection,
    titles: dict[str, str],
    root_ids: list[str],
    dry_run: bool,
) -> dict[str, int]:
    """최상위 항목을 data_source로 맞춥니다.

    Args:
        conn: 커넥션
        titles: 노드 ID → 이름
        root_ids: 최상위 항목 ID
        dry_run: True면 쓰지 않습니다

    Returns:
        dict[str, int]: 최상위 항목 ID → data_source.id. dry_run이면 빈 값
    """
    if dry_run:
        for root_id in root_ids:
            print(f"  {titles.get(root_id, root_id)[:38]}")
        return {}

    source_ids = {
        root_id: upsert_source(
            conn, "notion", root_id, titles.get(root_id, root_id), {"kind": "root"}
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
) -> None:
    """권한에서 빠졌거나 기준에서 밀려난 것을 지웁니다.

    Args:
        conn: 커넥션
        root_ids: 지금 적재 대상인 최상위 항목 ID
        page_ids: 지금 적재 대상인 페이지 ID
        known: 이미 등록된 노션 data_source
    """
    gone = [row["name"] for ext, row in known.items() if ext not in root_ids]
    if gone:
        print(f"대상에서 빠져 지움: {', '.join(gone)}")

    with conn.cursor() as cur:
        cur.execute(DELETE_GONE_SOURCES, {"root_ids": root_ids})
        dropped_sources = cur.rowcount
        cur.execute(DELETE_GONE_ITEMS, {"page_ids": page_ids})
        dropped_items = cur.rowcount
    conn.commit()

    if dropped_sources or dropped_items:
        print(f"삭제: 최상위 {dropped_sources}, 페이지 {dropped_items}")


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
