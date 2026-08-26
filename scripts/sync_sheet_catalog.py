"""
구글 시트 카탈로그 동기화

서비스 계정이 볼 수 있는 스프레드시트의 **이름·탭·머리행**을 지식베이스에
넣습니다. 그래야 query_knowledge 로 "출석 컬럼 있는 시트" 를 찾을 수 있습니다.

셀 값은 넣지 않습니다. 응답이 계속 쌓이는 시트라 값을 적재하면 곧 낡고,
낡은 숫자를 진실로 믿는 것이 값을 모르는 것보다 나쁩니다. 실제 값은
execute_python 안에서 실시간으로 읽습니다.

마지막 동기화 시각을 커서로 두고 **그 뒤에 수정된 파일만** 훑습니다.
셀 하나만 고쳐도 modifiedTime 은 갱신되므로 후보로는 자주 올라오지만,
머리행만 읽으므로 한 시트에 API 두 번이면 끝납니다.

**실패한 시트가 있으면 커서를 그 시트 앞으로만 밉니다.** 커서가 지나간 구간은
다시 훑지 않으므로, 실패를 삼키고 커서를 끝까지 밀면 그 시트는 다음에 누가
수정할 때까지 카탈로그에 없습니다. 그렇다고 커서를 통째로 붙잡아 두면 못 고치는
시트 하나가 드라이브 전체를 영영 멈춰 세웁니다. 그래서 **가장 오래된 실패 직전**
까지만 밉니다 -- 그 시트는 계속 재시도되고, 커서는 매번 조금씩이라도 나아갑니다.

  python3 scripts/sync_sheet_catalog.py --dry-run
  python3 scripts/sync_sheet_catalog.py --full     # 커서를 무시하고 전량
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from api.google_sheets import get_worksheet_headers, list_spreadsheet_files
from service.knowledge.db import connect
from service.knowledge.ingest import upsert_item
from service.knowledge.register import read_cursor, save_cursor, upsert_source
from service.sheets import catalog

# 커서를 조금 앞당겨 겹쳐 읽습니다. 동기화 도중에 저장된 파일이 커서 뒤로
# 밀려 영영 안 잡히는 일을 막습니다.
OVERLAP = timedelta(minutes=5)

# 카탈로그는 드라이브 전체가 대상이라 채널·페이지처럼 여럿으로 나뉘지 않습니다.
# data_source 한 행이 "서비스 계정이 보는 스프레드시트 전부" 를 가리킵니다.
SOURCE_KEY = "all"

# 이번에 못 본 시트를 카탈로그에서 지웁니다. **커서가 비어 전량을 훑은 실행에서만**
# 안전합니다 -- 증분 실행의 "못 봤다" 는 "안 바뀌었다" 는 뜻이기 때문입니다.
# 그래서 조건은 --full 플래그가 아니라 cursor 가 비었는지를 봅니다. 첫 실행도
# 커서가 없어 전량을 훑으므로 같이 정리됩니다.
DELETE_MISSING = """
DELETE FROM item
WHERE data_source_id = %(source_id)s
  AND NOT (external_id = ANY(%(alive)s))
"""


def stamp(moment: datetime) -> str:
    """Drive 의 modifiedTime 과 **같은 모양**으로 시각을 찍습니다.

    커서와 modifiedTime 을 문자열로 비교하므로 소수 자릿수가 다르면 비교가
    어긋납니다. Drive 는 밀리초 세 자리로 주는데 isoformat() 은 마이크로초
    여섯 자리이거나 아예 없습니다.
    """
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def collect(modified_after: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """훑을 시트의 머리행을 모읍니다. DB 는 건드리지 않습니다.

    구글 API 왕복을 트랜잭션 밖에서 끝내려고 갈라 두었습니다. 첫 전량 실행은
    몇 분이 걸리는데, 그동안 트랜잭션을 열어 두면 idle-in-transaction 이 됩니다.

    Args:
        modified_after: 이 시각 뒤에 수정된 것만. 비우면 전량

    Returns:
        tuple: (시트별 file·tabs 목록, 실패한 file 목록)
    """
    files = list_spreadsheet_files()
    if modified_after:
        files = [f for f in files if f.get("modifiedTime", "") > modified_after]

    collected: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for file in files:
        try:
            tabs = get_worksheet_headers(file["id"])
        except Exception as error:
            # 한 시트가 막혀도 나머지는 계속합니다. 다만 실패한 파일을 들고 나가서
            # 커서가 그 앞에서 멈추게 합니다 -- 다음 실행에 다시 후보가 됩니다.
            print(f"  ✖ {file['name']}: {type(error).__name__} {error}")
            failed.append(file)
            continue
        collected.append({"file": file, "tabs": tabs})
    return collected, failed


def next_cursor(started: datetime, failed: list[dict[str, Any]]) -> str:
    """다음 커서를 정합니다. 실패한 시트가 다시 후보가 되도록 그 앞에서 멈춥니다.

    Args:
        started: 이번 실행 시작 시각
        failed: 실패한 file 목록

    Returns:
        str: 저장할 커서
    """
    cursor = stamp(started - OVERLAP)
    if not failed:
        return cursor
    # 가장 오래된 실패 **직전**. 그 시트는 다음에도 후보로 잡히고, 그보다 앞선
    # 구간은 이미 성공했으므로 다시 볼 이유가 없습니다.
    oldest = min(file.get("modifiedTime", "") for file in failed)
    edge = stamp(
        datetime.fromisoformat(oldest.replace("Z", "+00:00")) - timedelta(seconds=1)
    )
    return min(cursor, edge)


def main(dry_run: bool = False, full: bool = False) -> None:
    """카탈로그를 동기화합니다.

    Args:
        dry_run: 적재하지 않고 무엇이 들어갈지만 출력. 언제나 전량이다
        full: 커서를 무시하고 전량 훑기
    """
    load_dotenv()
    started = datetime.now(tz=timezone.utc)

    if dry_run:
        # DB 를 건드리지 않는다. 구글 쪽만 확인하는 용도라 DB 가 없는 곳에서도 돈다.
        # 커서를 못 읽으므로 증분 경로는 미리보기할 수 없다.
        source_id, cursor = 0, ""
    else:
        with connect() as conn:
            source_id = upsert_source(
                conn, catalog.SOURCE, SOURCE_KEY, "구글 시트 카탈로그"
            )
            cursor = (
                "" if full else (read_cursor(conn, catalog.SOURCE, SOURCE_KEY) or "")
            )

    collected, failed = collect(cursor)
    print(
        f"훑은 시트 {len(collected)}개"
        + (f" (커서 {cursor} 이후)" if cursor else " (전량)")
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
        # 전량을 훑고, 전부 성공하고, 살아 있는 시트가 하나라도 있을 때만 지웁니다.
        # rows 가 비면 조건이 모든 행에 대해 참이라 카탈로그가 통째로 날아갑니다 --
        # 공유가 끊기거나 스코프가 회귀해 목록이 0건으로 오는 경우가 그렇습니다.
        if not cursor and not failed and rows:
            # 휴지통에 갔거나 공유가 끊긴 시트는 목록에 안 나옵니다. 남겨 두면
            # query_knowledge 가 계속 찾아 주고, 그다음 읽기가 권한 오류로 터집니다.
            removed = conn.execute(
                DELETE_MISSING,
                {"source_id": source_id, "alive": [r["external_id"] for r in rows]},
            ).rowcount
            print(f"카탈로그에서 지운 시트 {removed}개")

        if failed:
            print(
                "실패한 시트 앞에서 커서를 멈춥니다(다음 실행에 재시도):"
                f" {', '.join(file['name'] for file in failed)}"
            )
        save_cursor(conn, source_id, next_cursor(started, failed))

    print(f"새로 적재 {inserted} / 갱신 포함 {len(rows)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full", action="store_true", help="커서를 무시하고 전량")
    args = parser.parse_args()
    main(dry_run=args.dry_run, full=args.full)
