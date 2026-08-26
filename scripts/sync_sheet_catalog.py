"""
구글 시트 카탈로그 동기화

서비스 계정이 볼 수 있는 스프레드시트의 **이름·탭·머리행**을 지식베이스에
넣습니다. 그래야 query_knowledge 로 "출석 컬럼 있는 시트" 를 찾을 수 있습니다.

셀 값은 넣지 않습니다. 응답이 계속 쌓이는 시트라 값을 적재하면 곧 낡고,
낡은 숫자를 진실로 믿는 것이 값을 모르는 것보다 나쁩니다. 실제 값은
execute_python 안에서 실시간으로 읽습니다.

마지막 동기화 시각을 커서로 두고 **그 뒤에 수정된 파일만** 훑습니다.
셀 하나만 고쳐도 modifiedTime 은 갱신되므로 후보로는 자주 올라오지만,
머리행 한 줄만 읽어 해시를 비교하고 대부분 그대로 끝납니다.

  python3 scripts/sync_sheet_catalog.py --dry-run
  python3 scripts/sync_sheet_catalog.py --full     # 커서를 무시하고 전량
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from api.google_sheets import (
    get_worksheet_headers,
    list_spreadsheets_changed_since,
)
from service.knowledge.db import connect
from service.knowledge.ingest import upsert_item
from service.knowledge.register import upsert_source
from service.sheets import catalog

# 커서를 조금 앞당겨 겹쳐 읽습니다. 동기화 도중에 저장된 파일이 커서 뒤로
# 밀려 영영 안 잡히는 일을 막습니다.
OVERLAP = timedelta(minutes=5)

READ_CURSOR = """
SELECT id, cursor FROM data_source
WHERE source = %(source)s AND external_id = %(external_id)s
"""

SAVE_CURSOR = "UPDATE data_source SET cursor = %(cursor)s, last_synced_at = now() WHERE id = %(id)s"

# 카탈로그는 드라이브 전체가 대상이라 채널·페이지처럼 여럿으로 나뉘지 않는다.
# data_source 한 행이 "서비스 계정이 보는 스프레드시트 전부" 를 가리킨다.
SOURCE_KEY = "all"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full", action="store_true", help="커서를 무시하고 전량")
    args = parser.parse_args()

    load_dotenv()
    started = datetime.now(tz=timezone.utc)

    if args.dry_run:
        files = list_spreadsheets_changed_since("")
        print(f"수정된 시트 {len(files)}개 (dry-run 은 커서를 보지 않는다)")
        for file in files[:10]:
            tabs = get_worksheet_headers(file["id"])
            print(f"\n· {file['name']}  id={file['id']}")
            print(
                "  " + catalog.build_raw_text(file["name"], tabs).replace("\n", "\n  ")
            )
        if len(files) > 10:
            print(f"\n… 외 {len(files) - 10}개")
        print("\n(dry-run — 적재하지 않음)")
        return

    with connect() as conn:
        source_id = upsert_source(
            conn, catalog.SOURCE, SOURCE_KEY, "구글 시트 카탈로그"
        )
        row = conn.execute(
            READ_CURSOR, {"source": catalog.SOURCE, "external_id": SOURCE_KEY}
        ).fetchone()
        cursor = "" if args.full else (row.get("cursor") or "")

        files = list_spreadsheets_changed_since(cursor)
        print(
            f"수정된 시트 {len(files)}개"
            + (f" (커서 {cursor} 이후)" if cursor else " (전량)")
        )

        changed = 0
        for file in files:
            try:
                tabs = get_worksheet_headers(file["id"])
            except Exception as error:
                # 한 시트가 막혀도 나머지는 계속한다. 권한이 빠진 파일이 섞인다.
                print(f"  ✖ {file['name']}: {type(error).__name__} {error}")
                continue
            result = upsert_item(conn, catalog.build_row(source_id, file, tabs))
            if result["inserted"]:
                changed += 1
                print(f"  + {file['name']}")

        # 커서는 훑기 시작한 시각에서 겹침만큼 뺀 값이다. 지금 시각으로 두면
        # 훑는 동안 저장된 파일을 놓친다.
        conn.execute(
            SAVE_CURSOR,
            {
                "id": source_id,
                "cursor": (started - OVERLAP).isoformat().replace("+00:00", "Z"),
            },
        )
        print(f"\n새로 적재 {changed} / 훑은 시트 {len(files)}")


if __name__ == "__main__":
    main()
