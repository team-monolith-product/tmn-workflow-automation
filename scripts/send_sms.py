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
    parser.add_argument("--dry-run", action="store_true", help="발송하지 않고 확인만")
    args = parser.parse_args()

    rows = [
        {
            "to": args.to or "",
            "name": args.name,
            **{key: getattr(args, key) for key in VAR_KEYS},
        }
    ]
    summary = sms_send.preview(rows, args.content)
    if summary["problems"]:
        print("보내기 전에 고칠 것:")
        for problem in summary["problems"]:
            print(f"  - {problem}")
        raise SystemExit(1)

    print(
        f"{summary['message_type']} · 최대 {summary['max_bytes']}byte"
        f" · 대상 {summary['targets']}명"
    )
    print("-" * 60)
    print(summary["sample"])
    print("-" * 60)

    if args.dry_run:
        print("(dry-run — 발송하지 않음)")
        return

    result = sms_send.send(rows=rows, content=args.content, subject=args.subject)
    print(
        f"접수 완료 — {result['sent']}명\nmessageKey {result['message_key']}\n"
        "접수 성공이지 도달 확인이 아닙니다."
    )


if __name__ == "__main__":
    main()
