"""
문자를 보내고 발송이력 시트에 남깁니다.

슬랙 승인 카드와 MCP 도구는 별도 PR 로 붙습니다. 이 스크립트는 그 전에
발송 계층을 사람이 직접 확인하는 손잡이입니다.

같은 campaign 으로 이미 보낸 번호는 자동으로 빠지므로, 여러 번 돌려도
같은 사람에게 두 번 가지 않습니다. 재발송이 필요하면 campaign 을 바꿉니다.

사용법:
    # 문안·타입·길이만 확인 (발송하지 않음)
    python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \\
        --template discord --to 010-1111-1111 --name 홍길동 \\
        --var1 "1기 (서울)" --var2 https://discord.gg/xxx --dry-run

    # 명단 파일로 (헤더: to,name,var1..var8)
    python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \\
        --template discord --csv roster.csv --dry-run

    # 실제 발송
    python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \\
        --template discord --csv roster.csv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import csv
import datetime
import pathlib
import re

from dotenv import load_dotenv

from service.sms import send as sms_send
from service.sms.templates import VAR_KEYS

SPREADSHEET_URL_HINT = "https://docs.google.com/spreadsheets/d/<ID>/edit"


def read_csv(path: pathlib.Path) -> list[dict]:
    """명단 파일을 읽습니다.

    Args:
        path: to·name·var1~var8 헤더를 가진 CSV

    Returns:
        list[dict]: 수신자 목록
    """
    with path.open(encoding="utf-8-sig") as file:
        # 빈 값을 걸러내지 않는다. 번호 칸이 빈 행에서 to 키까지 사라지면
        # check 가 문제를 모아 돌려주는 대신 KeyError 로 죽는다.
        return list(csv.DictReader(file))


def spreadsheet_id(value: str) -> str:
    """주소를 붙여넣어도 ID 를 뽑습니다.

    Args:
        value: 스프레드시트 주소 또는 ID

    Returns:
        str: 스프레드시트 ID

    Raises:
        ValueError: 어느 쪽으로도 읽히지 않을 때. 그냥 통과시키면 엉뚱한 ID 로
            시트를 열려다 발송 직전에 죽는다
    """
    found = value.strip().split("/spreadsheets/d/")[-1].split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", found):
        raise ValueError(f"스프레드시트 주소나 ID 로 읽히지 않습니다: {value}")
    return found


def main() -> None:
    """명령행 인자를 파싱해 발송합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="문자 발송")
    parser.add_argument(
        "--spreadsheet",
        required=True,
        help=f"발송이력을 적을 시트 ({SPREADSHEET_URL_HINT})",
    )
    parser.add_argument("--campaign", required=True, help="발송 건 식별자")
    parser.add_argument("--template", help="templates/sms/<이름>.txt")
    parser.add_argument("--content", help="즉석 문안 본문")
    parser.add_argument("--subject", help="LMS 제목. 생략하면 campaign")
    parser.add_argument("--csv", type=pathlib.Path, help="명단 파일")
    parser.add_argument("--to", help="수신번호 한 명")
    parser.add_argument("--name", default="", help="--to 의 이름")
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

    if bool(args.csv) == bool(args.to):
        parser.error("--csv 와 --to 중 하나만 주세요.")

    # 발송 인자 자리에서 평가하면 dry-run 이 통과시킨 명령이 실발송에서만
    # 터진다. 이 CLI 가 없애려는 게 정확히 그 왕복이다.
    sheet_id = spreadsheet_id(args.spreadsheet)

    if args.csv:
        rows = read_csv(args.csv)
    else:
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
    problems = sms_send.check(
        rows, template_name=args.template, content=args.content, send_at=send_at
    )
    if problems:
        # 하나씩 터뜨리면 고치고 다시 돌리고를 반복하게 된다.
        print("보내기 전에 고칠 것:")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(1)

    summary = sms_send.preview(rows, args.template, args.content)
    print(
        f"{summary['message_type']} · 치환 후 최대 {summary['max_bytes']}byte"
        f" · 대상 {summary['targets']}명"
        + (f" · 중복 {summary['folded']}건 접음" if summary["folded"] else "")
        + (f" · 예약 {args.at}" if args.at else " · 즉시 발송")
    )
    print("-" * 60)
    print(summary["sample"])
    print("-" * 60)

    if args.dry_run:
        print("(dry-run — 발송하지 않음)")
        return

    result = sms_send.send_campaign(
        spreadsheet_id=sheet_id,
        campaign=args.campaign,
        rows=rows,
        template_name=args.template,
        content=args.content,
        subject=args.subject,
        send_at=send_at,
        requested_by="script",
        entrypoint="script",
    )
    if result["sent"] == 0:
        print(
            f"대상 {result['requested']}명이 모두 이미 발송된 상태라 보내지 않았습니다."
        )
        return
    print(
        f"접수 완료 — 발송 {result['sent']}명"
        + (f" · 중복 제외 {result['skipped']}명" if result["skipped"] else "")
        + f"\nmessageKey {result['message_key']}"
        + (
            f"\n예약됨 {args.at} — 취소는 `--cancel {result['message_key']}` (발송 1분 전까지)"
            if args.at
            else "\n접수 성공이지 도달 확인이 아닙니다. 도달 결과는 뿌리오 웹에서 확인하세요."
        )
    )


if __name__ == "__main__":
    main()
