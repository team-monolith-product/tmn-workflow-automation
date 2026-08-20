"""
문자를 보냅니다. 슬랙 없이 발송 계층만 확인하는 손잡이입니다.

사용법:
    # 문안·타입·길이만 확인 (발송하지 않음)
    python scripts/send_sms.py --content "[*이름*]선생님, 안내드립니다" \\
        --to 010-1111-1111 --name 홍길동 --dry-run

    # 실제 발송
    python scripts/send_sms.py --content "..." --to 010-1111-1111 --name 홍길동
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import datetime

from dotenv import load_dotenv

from service.sms import send as sms_send
from service.sms.templates import VAR_KEYS


def main() -> None:
    """명령행 인자를 파싱해 발송합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="문자 발송")
    parser.add_argument("--content", help="문안 본문")
    parser.add_argument("--to", help="수신번호")
    parser.add_argument("--name", default="", help="[*이름*] 치환값")
    parser.add_argument("--subject", help="LMS 제목")
    for key in VAR_KEYS:
        parser.add_argument(f"--{key}", default="", help=f"[*{key[3:]}*] 치환값")
    parser.add_argument(
        "--at", metavar="'2026-08-13 09:00'", help="예약 발송 시각. 최소 3분 뒤"
    )
    parser.add_argument("--cancel", metavar="MESSAGE_KEY", help="예약 취소")
    parser.add_argument("--dry-run", action="store_true", help="발송하지 않고 확인만")
    args = parser.parse_args()

    if args.cancel:
        print(sms_send.cancel_reserved(args.cancel))
        return

    if not args.to:
        parser.error("--to 가 필요합니다.")

    rows = [
        {
            "to": args.to,
            "name": args.name,
            **{key: getattr(args, key) for key in VAR_KEYS},
        }
    ]
    send_at = (
        datetime.datetime.fromisoformat(args.at.replace("/", "-")) if args.at else None
    )

    problems = sms_send.check(rows, args.content, send_at)
    if problems:
        print("보내기 전에 고칠 것:")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(1)

    summary = sms_send.preview(rows, args.content)
    print(
        f"{summary['message_type']} · 치환 후 최대 {summary['max_bytes']}byte"
        f" · 대상 {summary['targets']}명"
        + (f" · 예약 {args.at}" if args.at else " · 즉시 발송")
    )
    print("-" * 60)
    print(summary["sample"])
    print("-" * 60)

    if args.dry_run:
        print("(dry-run — 발송하지 않음)")
        return

    result = sms_send.send(
        rows=rows, content=args.content, subject=args.subject, send_at=send_at
    )
    print(
        f"접수 완료 — {result['sent']}명\nmessageKey {result['message_key']}\n"
        "접수 성공이지 도달 확인이 아닙니다."
    )


if __name__ == "__main__":
    main()
