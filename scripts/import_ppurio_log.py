"""
기존 뿌리오 발송 로그(ppurio_log.jsonl)를 발송이력 시트로 옮깁니다.

이 이관을 건너뛰면 이미 문자를 받은 분들이 "아직 안 보낸 사람"으로 보여서
재발송됩니다. 새 경로를 켜기 전에 반드시 한 번 돌려야 합니다.

기존 로그의 msg 필드가 캠페인이 됩니다. 같은 (캠페인, 번호)가 여러 번 있으면
첫 건만 넣습니다 — 재발송분은 어차피 중복 차단 대상이라 한 행이면 충분합니다.

사용법:
    python scripts/import_ppurio_log.py --file ~/Workspace/repo/industry-linked/ppurio_log.jsonl --dry-run
    python scripts/import_ppurio_log.py --file ~/Workspace/repo/industry-linked/ppurio_log.jsonl
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import pathlib
from collections import Counter

from dotenv import load_dotenv

from api import google_sheets
from service.sms import ledger


def read_log(path: pathlib.Path) -> list[dict]:
    """jsonl 을 읽어 행 목록으로 만듭니다.

    Args:
        path: ppurio_log.jsonl 경로

    Returns:
        list[dict]: 로그 한 줄이 한 항목
    """
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def to_row(entry: dict, requested_by: str) -> list:
    """로그 한 줄을 발송이력 행으로 바꿉니다.

    Args:
        entry: 로그 항목
        requested_by: 이관분에 남길 요청자

    Returns:
        list: HEADER 순서의 값 목록
    """
    return [
        entry["ts"],
        entry["msg"],
        entry["to"],
        entry.get("name", ""),
        entry.get("type", "LMS"),
        entry.get("messageKey", ""),
        entry.get("code", ""),
        "",
        requested_by,
        "script",
    ]


def main() -> None:
    """명령행 인자를 파싱해 이관을 실행합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="뿌리오 발송 로그 이관")
    parser.add_argument("--file", required=True, type=pathlib.Path)
    parser.add_argument("--requested-by", default="imported@team-mono.com")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    entries = read_log(args.file.expanduser())
    accepted = [e for e in entries if e.get("code") == "1000"]

    seen: set[tuple[str, str]] = set()
    unique = []
    for entry in accepted:
        key = (entry["msg"], entry["to"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)

    print(
        f"로그 {len(entries)}건 · 접수성공 {len(accepted)}건 · 중복 제외 {len(unique)}건"
    )
    for campaign, count in Counter(e["msg"] for e in unique).most_common():
        print(f"  {campaign:<12} {count}")

    if args.dry_run:
        print("\n(dry-run — 적재하지 않음)")
        return

    ws = ledger.open_ledger()
    already = {
        (row.get("캠페인", ""), row.get("번호", "")) for row in ledger.read_rows(ws)
    }
    rows = [
        to_row(e, args.requested_by)
        for e in unique
        if (e["msg"], e["to"]) not in already
    ]

    if not rows:
        print("\n이미 전부 들어가 있습니다.")
        return

    google_sheets.append_rows(ws, rows)
    print(f"\n{len(rows)}건 적재 · 기존 {len(unique) - len(rows)}건은 이미 있음")


if __name__ == "__main__":
    main()
