"""
문자를 보내고 참가자 명단 시트의 캠페인 열에 표시합니다.

슬랙 승인 카드와 MCP 도구는 별도 PR 로 붙습니다. 이 스크립트는 그 전에
발송 계층을 사람이 직접 확인하는 손잡이입니다.

명단에서 그 캠페인 열이 빈 사람만 보냅니다. 여러 번 돌려도 같은 사람에게 두 번
가지 않습니다. 재발송이 필요하면 campaign 을 바꾸거나 그 칸을 지웁니다.

공식 문자만 여기로 보냅니다. 개인 CS 문자는 슬랙 스레드가 기록입니다.

사용법:
    # 문안·타입·길이만 확인 (발송하지 않음)
    python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \\
        --content "[*이름*]선생님, 안내드립니다" --to 010-1111-1111 --name 홍길동 --dry-run

    # 명단 파일로 (헤더: to,name,var1..var8)
    python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \\
        --content "..." --csv roster.csv --dry-run

    # 실제 발송
    python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \\
        --content "..." --csv roster.csv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import csv
import datetime
import pathlib

from dotenv import load_dotenv

from service.sms import ledger
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
        # restval 도 같은 이유다. 기본값 None 이면 짧은 행에서 to 가 None 이 되어
        # 정규식이 TypeError 로 죽는다. 빈 문자열이면 검사 경로로 들어온다.
        reader = csv.DictReader(file, restval="")
        fields = reader.fieldnames or []
        # to 만 보면 나머지 열 이름이 틀렸을 때 changeWord 가 통째로 안 실리고,
        # 수신자는 [*1*] 자리가 빈 문자를 받는다. 관문을 전부 통과하고 발송까지
        # 끝난 뒤에야 알게 되며, 그 campaign 은 이미 잡혀 정정 발송도 막힌다.
        allowed = {"to", "name", *VAR_KEYS}
        unknown = [name for name in fields if name not in allowed]
        if "to" not in fields or unknown:
            raise ValueError(
                f"CSV 헤더가 맞지 않습니다: {fields}\n"
                f"기대: to,name,{','.join(VAR_KEYS)}\n"
                "참가자 시트를 그대로 내보내면 헤더가 번호,이름 이라 맞지 않습니다. "
                "치환값 열은 var1~var8 로 바꿔주세요."
            )
        return list(reader)


def main() -> None:
    """명령행 인자를 파싱해 발송합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="문자 발송")
    # --cancel 은 이 둘을 쓰지 않는다. required 로 두면 예약을 잘못 걸었다는 걸
    # 깨달은 사람이 안 쓰이는 인자를 지어내는 동안 문자가 나간다.
    parser.add_argument(
        "--spreadsheet", help=f"참가자 명단 시트 ({SPREADSHEET_URL_HINT})"
    )
    parser.add_argument(
        "--worksheet", help="명단 탭 이름. 생략하면 주소의 gid, 그것도 없으면 첫 탭"
    )
    parser.add_argument("--campaign", help="발송 건 식별자")
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
        # 취소하려는 사람은 방금 돌린 발송 명령줄을 복사해 --cancel 을 붙인다.
        # 그 줄에 --dry-run 이 남아 있으면 확인만 하려던 명령이 예약을 실제로
        # 취소하고, 이력 행은 남으므로 같은 campaign 으로 다시 예약도 안 된다.
        if args.dry_run:
            parser.error("--cancel 은 --dry-run 과 함께 쓸 수 없습니다.")
        print(sms_send.cancel_reserved(args.cancel))
        return

    for name in ("spreadsheet", "campaign"):
        if not getattr(args, name):
            parser.error(f"--{name} 이 필요합니다.")

    if bool(args.csv) == bool(args.to):
        parser.error("--csv 와 --to 중 하나만 주세요.")

    # 발송 인자 자리에서 평가하면 dry-run 이 통과시킨 명령이 실발송에서만
    # 터진다. 이 CLI 가 없애려는 게 정확히 그 왕복이다.
    sheet_id = ledger.parse_spreadsheet_id(args.spreadsheet)
    gid = ledger.parse_gid(args.spreadsheet)

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
        worksheet=args.worksheet,
        gid=gid,
    )
    if result["missing"]:
        print(
            f"명단에 없어 보내지 않은 번호 {len(result['missing'])}건: "
            + ", ".join(result["missing"])
        )
    if result["sent"] == 0:
        # 이유를 뭉뚱그리면 안 된다. 번호 열을 잘못 잡아 전원이 명단 밖으로
        # 빠진 것을 "이미 발송됨"으로 읽으면, 아무에게도 안 나간 캠페인을
        # 끝난 것으로 알고 넘어간다.
        if result["skipped"]:
            print(f"이미 발송된 {result['skipped']}명을 제외하니 보낼 사람이 없습니다.")
        else:
            print(
                f"대상 {result['requested']}명이 모두 명단에 없어 한 통도 "
                "보내지 않았습니다. 번호 표기와 명단의 번호 열을 확인하세요."
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
