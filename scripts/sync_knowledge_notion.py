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
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import psycopg
from dotenv import load_dotenv
from notion_client.errors import APIResponseError

from app.common import notion, notion_page_to_markdown
from app.knowledge import DISTILL_DELAY_SECONDS
from service.knowledge.db import connect, fetch_all
from service.knowledge.ingest import upsert_item
from service.knowledge.notion import (
    MIN_BODY_CHARS,
    build_fetch_node,
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

# Export 파일 이름 끝의 페이지 ID.
EXPORT_ID = re.compile(r" ([0-9a-f]{32})\.md$")

# Export 마크다운에서 제목 다음에 오는 "이름: 값" 줄.
PROPERTY_LINE = re.compile(r"^[^\n:]{1,40}: ")

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

# 이미 행이 색인된 데이터베이스. 부모는 build_page_row가 metadata에 남긴다.
INDEXED_DATABASES = """
SELECT DISTINCT item.metadata->'parent'->>'data_source_id' AS data_source_id
FROM item
JOIN data_source ON data_source.id = item.data_source_id
WHERE data_source.source = 'notion'
  AND item.metadata->'parent'->>'data_source_id' IS NOT NULL
"""


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
    except APIResponseError as exc:
        # 상태를 남긴다. 종류 이름만 찍으면 권한 밖(404)과 한도 초과(429)가
        # 같은 줄로 보이는데, 앞은 본문이 없는 것이 맞고 뒤는 모르는 것이다.
        print(f"  본문 실패 {page_id}: {exc.status} {exc.code}")
        return ""


def load_export(export_root: Path) -> dict[str, Path]:
    """Export 디렉터리에서 페이지 ID → 파일 경로를 만듭니다.

    노션은 내보낸 파일 이름 끝에 32자리 페이지 ID를 붙입니다. 그래서 실시간
    수집과 같은 external_id를 파일명만으로 얻습니다.

    Args:
        export_root: 압축을 푼 디렉터리

    Returns:
        dict[str, Path]: 붙임표 없는 페이지 ID → .md 파일
    """
    files = {
        match.group(1): path
        for path in export_root.rglob("*.md")
        if (match := EXPORT_ID.search(path.name))
    }
    print(f"Export 파일 {len(files)}개")
    return files


def export_body(path: Path) -> str:
    """Export 마크다운에서 제목과 속성 블록을 떼고 본문만 남깁니다.

    떼지 않으면 표의 행도 속성만으로 200자를 넘겨 전부 문서로 판정됩니다.
    속성 블록은 제목 다음의 "이름: 값" 줄이 이어지는 구간입니다.

    Args:
        path: .md 파일

    Returns:
        str: 본문
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    index = 1 if lines and lines[0].startswith("# ") else 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    while (
        index < len(lines)
        and lines[index].strip()
        and PROPERTY_LINE.match(lines[index])
    ):
        index += 1
    return "\n".join(lines[index:]).strip()


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


def sync(
    dsn: str | None, full: bool, dry_run: bool, export_root: Path | None = None
) -> None:
    """통합 권한 전체를 동기화합니다.

    Args:
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL
        full: True면 last_synced_at을 무시하고 전부 다시 받습니다
        dry_run: True면 아무것도 쓰지 않고 대상만 셉니다
        export_root: 워크스페이스 Export를 푼 디렉터리. 주면 본문을 API 대신
            거기서 읽습니다. Export에 없는 페이지는 이번 회차에서 빠집니다
    """
    client = notion
    export = load_export(export_root) if export_root else {}
    user_emails = fetch_user_emails(client)

    nodes = fetch_accessible(client)
    by_id = {node["id"]: node for node in nodes}
    # 사슬을 잇느라 받아온 조상. 최상위 이름을 찾을 때도 쓴다.
    fetched: dict[str, dict | None] = {}

    roots = derive_roots(nodes, fetch_node=build_fetch_node(client, fetched))
    # 최상위가 search에 없던 노드일 수 있다. "회의록"처럼 워크스페이스 직속
    # 데이터베이스가 그렇다.
    resolved = {**{k: v for k, v in fetched.items() if v}, **by_id}
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

    titles = {node_id: node_title(node) for node_id, node in resolved.items()}

    if export:
        # Export가 있으면 행마다 직접 잰다. 표본 판정은 데이터베이스 하나를
        # 여덟 행으로 가르는 근사였고, 본문이 파일로 있으면 근사할 이유가 없다.
        exported = [page for page in pages if page["id"].replace("-", "") in export]
        candidates = [
            page
            for page in exported
            if not is_database_row(page)
            or len(export_body(export[page["id"].replace("-", "")])) >= MIN_BODY_CHARS
        ]
        print(
            f"Export에 없어 이번 회차에서 빠짐 {len(pages) - len(exported)}개, "
            f"본문이 {MIN_BODY_CHARS}자 미만이라 제외 {len(exported) - len(candidates)}개"
        )
    else:
        # 이미 색인에 있는 데이터베이스는 판정 대상이 아니다. 표본 여덟 행이
        # 표라고 해도, Export로 행마다 재서 넣은 것을 지울 근거는 되지 않는다.
        with connect(dsn) as conn:
            indexed = {
                row["data_source_id"] for row in fetch_all(conn, INDEXED_DATABASES)
            }
        documental = classify_databases(rows_by_database, titles) | (
            indexed & set(rows_by_database)
        )
        candidates = list(documents)
        for database_id in documental:
            candidates.extend(rows_by_database[database_id])

    # 최상위를 못 찾은 페이지는 넣지 않는다. 사슬이 중간에 끊기면 최상위가
    # 블록이나 지워진 데이터베이스의 ID가 되는데, 그것으로 data_source를
    # 만들면 이름도 실체도 없는 출처가 검색 결과에 찍힌다.
    targets = [page for page in candidates if roots[page["id"]] in resolved]
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

            exported = export.get(page["id"].replace("-", ""))
            markdown = export_body(exported) if exported else body_of(page["id"])
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

        if export:
            # Export 회차는 넣기만 하고 지우지 않습니다. 삭제 판정은 전량 열거를
            # 전제로 하는데 Export는 그것이 아닙니다. 실측에서 권한 안 페이지
            # 84,660개 중 5,220개가 Export에 없었습니다. last_synced_at도 찍지
            # 않습니다. 찍으면 다음 회차가 그 빈자리를 건너뜁니다.
            print("Export 회차라 삭제와 last_synced_at을 건너뜁니다")
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
    parser.add_argument(
        "--export-root", type=Path, help="워크스페이스 Export를 푼 디렉터리"
    )
    args = parser.parse_args()

    sync(args.dsn, args.full, args.dry_run, args.export_root)


if __name__ == "__main__":
    main()
