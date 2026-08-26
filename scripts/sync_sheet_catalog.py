"""
구글 시트 카탈로그 동기화

서비스 계정이 볼 수 있는 스프레드시트의 **이름·탭·머리행**을 지식베이스에
넣습니다. 그래야 query_knowledge 로 "출석 컬럼 있는 시트" 를 찾을 수 있습니다.

셀 값은 넣지 않습니다. 응답이 계속 쌓이는 시트라 값을 적재하면 곧 낡고,
낡은 숫자를 진실로 믿는 것이 값을 모르는 것보다 나쁩니다. 실제 값은
execute_python_with_chart 안에서 실시간으로 읽습니다.

**전역 커서를 두지 않습니다.** Drive 목록은 어차피 매번 전량으로 오고,
아낄 수 있는 것은 시트마다 드는 머리행 읽기뿐입니다. 그 판단에 필요한 값은
이미 시트별로 있습니다 -- 지난번에 적재한 modifiedTime 이 item.source_updated_at
에 들어 있어서, 그것과 같은 파일만 건너뛰면 됩니다.

시트별로 보는 덕에 **실패한 시트는 저절로 재시도됩니다.** 저장된 값이 갱신되지
않았으니 다음 실행에도 후보로 올라옵니다. 못 고치는 시트가 하나 있어도 나머지는
계속 최신으로 유지되고, 사라진 시트 정리도 매 실행 돕니다.

  python3 scripts/sync_sheet_catalog.py --dry-run
  python3 scripts/sync_sheet_catalog.py --full     # 안 바뀐 시트까지 전부 다시
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import time
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from api.google_sheets import (
    READS_PER_SHEET,
    get_worksheet_headers,
    list_spreadsheet_files,
)
from service.knowledge.db import connect
from service.knowledge.ingest import upsert_item
from service.knowledge.register import upsert_source
from service.sheets import catalog

# Sheets 읽기 쿼터는 사용자당 분당 60회고 서비스 계정 하나가 곧 한 명입니다.
# 사람이 부르는 read_sheet 가 쓸 몫을 남겨야 하므로 절반만 씁니다.
PACE_SECONDS = 60.0 / (30 / READS_PER_SHEET)

# 카탈로그는 드라이브 전체가 대상이라 채널·페이지처럼 여럿으로 나뉘지 않습니다.
# data_source 한 행이 "서비스 계정이 보는 스프레드시트 전부" 를 가리킵니다.
SOURCE_KEY = "all"

# 지난번에 적재한 시트와 그때의 수정 시각. 이것이 커서를 대신합니다.
READ_KNOWN = """
SELECT external_id, source_updated_at FROM item WHERE data_source_id = %(source_id)s
"""

# 목록에 없는 시트를 카탈로그에서 지웁니다. alive 는 이번 목록에 나온 파일 **전부**
# 라서, 머리행 읽기에 실패한 시트나 안 바뀌어 건너뛴 시트도 살아 있는 것으로 셉니다.
# 그 둘을 빼면 멀쩡한 시트를 지웁니다.
DELETE_MISSING = """
DELETE FROM item
WHERE data_source_id = %(source_id)s
  AND NOT (external_id = ANY(%(alive)s))
"""


def collect(
    files: list[dict[str, Any]], known: dict[str, datetime]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """바뀐 시트의 머리행을 모읍니다. DB 는 건드리지 않습니다.

    구글 API 왕복을 트랜잭션 밖에서 끝내려고 갈라 두었습니다. 첫 전량 실행은
    몇 분이 걸리는데, 그동안 트랜잭션을 열어 두면 idle-in-transaction 이 됩니다.

    **읽기 사이에 쉽니다.** 안 그러면 94개 전량 훑기가 분당 100회를 넘겨 429 를
    맞습니다(8/26 실측: 30개 중 10개). 재시도로는 못 풉니다 -- 같은 쿼터를 사람이
    부르는 read_sheet 도 쓰므로, 여기서 다 먹으면 봇이 시트를 못 읽습니다.

    Args:
        files: Drive 가 준 스프레드시트 목록
        known: 지난번에 적재한 {시트 ID: 그때의 수정 시각}. 비우면 전량

    Returns:
        tuple: (시트별 file·tabs 목록, 실패한 file 목록, 안 바뀌어 건너뛴 수)
    """
    collected: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped = 0
    read = 0
    for file in files:
        if known.get(file["id"]) == catalog.modified_at(file):
            skipped += 1
            continue
        if read:
            time.sleep(PACE_SECONDS)
        read += 1
        try:
            tabs = get_worksheet_headers(file["id"])
        except Exception as error:
            # 한 시트가 막혀도 나머지는 계속합니다. 저장값이 안 바뀌므로 다음
            # 실행에 저절로 다시 후보가 됩니다 -- 재시도 장치가 따로 없습니다.
            print(f"  ✖ {file['name']}: {type(error).__name__} {error}")
            failed.append(file)
            continue
        collected.append({"file": file, "tabs": tabs})
    return collected, failed, skipped


def main(dry_run: bool = False, full: bool = False) -> None:
    """카탈로그를 동기화합니다.

    Args:
        dry_run: 적재하지 않고 무엇이 들어갈지만 출력. 언제나 전량이다
        full: 안 바뀐 시트까지 전부 다시 읽는다
    """
    load_dotenv()

    if dry_run:
        # DB 를 건드리지 않는다. 구글 쪽만 확인하는 용도라 DB 가 없는 곳에서도 돈다.
        source_id, known = 0, {}
    else:
        with connect() as conn:
            source_id = upsert_source(
                conn, catalog.SOURCE, SOURCE_KEY, "구글 시트 카탈로그"
            )
            known = (
                {}
                if full
                else {
                    row["external_id"]: row["source_updated_at"]
                    for row in conn.execute(READ_KNOWN, {"source_id": source_id})
                }
            )

    files = list_spreadsheet_files()
    collected, failed, skipped = collect(files, known)
    print(
        f"목록 {len(files)}개 · 훑음 {len(collected)}"
        + (f" · 그대로 {skipped}" if skipped else "")
        + (f" · 실패 {len(failed)}" if failed else "")
    )

    rows = [
        catalog.build_row(source_id, item["file"], item["tabs"]) for item in collected
    ]
    if dry_run:
        for row in rows[:10]:
            print(f"\n· {row['title']}  id={row['external_id']}")
            print("  " + row["raw_text"].replace("\n", "\n  "))
        if len(rows) > 10:
            print(f"\n… 외 {len(rows) - 10}개")
        print("\n(dry-run — 적재하지 않음)")
        return

    with connect() as conn:
        inserted = sum(1 for row in rows if upsert_item(conn, row)["inserted"])
        # 목록이 0건으로 오면(공유 해제·스코프 회귀) 카탈로그가 통째로 날아간다.
        if files:
            # 휴지통에 갔거나 공유가 끊긴 시트는 목록에 안 나옵니다. 남겨 두면
            # query_knowledge 가 계속 찾아 주고, 그다음 읽기가 권한 오류로 터집니다.
            removed = conn.execute(
                DELETE_MISSING,
                {"source_id": source_id, "alive": [file["id"] for file in files]},
            ).rowcount
            if removed:
                print(f"카탈로그에서 지운 시트 {removed}개")

    if failed:
        print(
            "다음 실행에 다시 시도합니다:"
            f" {', '.join(file['name'] for file in failed)}"
        )
    print(f"새로 적재 {inserted} / 갱신 포함 {len(rows)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--full", action="store_true", help="안 바뀐 시트까지 전부 다시 읽는다"
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, full=args.full)
