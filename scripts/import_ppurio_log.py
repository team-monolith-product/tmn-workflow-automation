"""
기존 뿌리오 발송 로그(ppurio_log.jsonl)를 sms_send 로 옮깁니다.

이 이관을 건너뛰면 이미 문자를 받은 분들이 "아직 안 보낸 사람"으로 보여서
재발송됩니다. 새 경로를 켜기 전에 반드시 한 번 돌려야 합니다.

기존 로그의 msg 필드가 campaign 이 됩니다. 같은 (campaign, phone) 이 여러 번
있으면 첫 건만 남습니다 — 재발송분은 어차피 중복 차단 대상이라 한 행이면
충분합니다.

content_hash 는 남길 수 없습니다. 그때 실제로 나간 본문이 코드 안 f-string
이었고 이후 문안이 바뀌었기 때문입니다. 이관분은 'imported' 로 표시합니다.

사용법:
    python scripts/import_ppurio_log.py --file ~/repo/industry-linked/ppurio_log.jsonl --dry-run
    python scripts/import_ppurio_log.py --file ~/repo/industry-linked/ppurio_log.jsonl
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import pathlib

from dotenv import load_dotenv

from service.knowledge.db import connect

INSERT = """
INSERT INTO sms_send (
    campaign, phone, name, message_type, content_hash,
    message_key, accepted_code, requested_by, entrypoint, created_at
)
VALUES (
    %(campaign)s, %(phone)s, %(name)s, %(message_type)s, 'imported',
    %(message_key)s, %(code)s, %(requested_by)s, 'script', %(created_at)s
)
ON CONFLICT (campaign, phone) DO NOTHING
RETURNING id
"""


def read_log(path: pathlib.Path) -> list[dict]:
    """jsonl 을 읽어 행 목록으로 만듭니다.

    Args:
        path: ppurio_log.jsonl 경로

    Returns:
        list[dict]: 로그 한 줄이 한 항목
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def to_params(entry: dict, requested_by: str) -> dict:
    """로그 한 줄을 INSERT 파라미터로 바꿉니다.

    Args:
        entry: 로그 항목
        requested_by: 이관분에 남길 요청자

    Returns:
        dict: INSERT 바인딩
    """
    return {
        "campaign": entry["msg"],
        "phone": entry["to"],
        "name": entry.get("name"),
        "message_type": entry.get("type") or "LMS",
        "message_key": entry.get("messageKey"),
        "code": entry.get("code"),
        "requested_by": requested_by,
        "created_at": entry["ts"],
    }


def main() -> None:
    """명령행 인자를 파싱해 이관을 실행합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="뿌리오 발송 로그 이관")
    parser.add_argument("--file", required=True, type=pathlib.Path)
    parser.add_argument(
        "--requested-by",
        default="imported@team-mono.com",
        help="이관분에 남길 요청자",
    )
    parser.add_argument("--dsn", help="접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    entries = read_log(args.file.expanduser())
    sent = [e for e in entries if e.get("code") == "1000"]
    print(f"로그 {len(entries)}건 · 접수성공 {len(sent)}건이 이관 대상")

    from collections import Counter

    for campaign, count in Counter(e["msg"] for e in sent).most_common():
        print(f"  {campaign:<12} {count}")

    if args.dry_run:
        print("\n(dry-run — 적재하지 않음)")
        return

    inserted = 0
    with connect(args.dsn) as conn:
        with conn.cursor() as cur:
            for entry in sent:
                cur.execute(INSERT, to_params(entry, args.requested_by))
                if cur.fetchone():
                    inserted += 1
        conn.commit()

    print(f"\n신규 {inserted}건 · 중복 {len(sent) - inserted}건")


if __name__ == "__main__":
    main()
