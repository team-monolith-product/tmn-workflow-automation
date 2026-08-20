"""
문자를 보내고 sms_send 에 기록합니다.

슬랙 승인 카드와 MCP 도구는 별도 PR 로 붙습니다. 이 스크립트는 그 전에
발송 계층을 사람이 직접 확인하는 손잡이입니다.

같은 campaign 으로 여러 번 돌려도 같은 사람에게 두 번 가지 않습니다.
개인 CS 는 --cs 로 보내며 중복 차단을 받지 않습니다.

사용법:
    # 문안·타입·길이만 확인 (발송하지 않음)
    python scripts/send_sms.py --campaign discord \\
        --content "[*이름*]선생님, 안내드립니다" --to 010-1111-1111 --name 홍길동 --dry-run

    # 명단 파일로 (헤더: to,name,var1..var8)
    python scripts/send_sms.py --campaign discord --content "..." --csv roster.csv

    # 그 번호에게 뭘 보냈는지
    python scripts/send_sms.py --history 010-1111-1111
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import csv
import datetime
import io
import pathlib

from dotenv import load_dotenv

from service.sms import log
from service.sms import send as sms_send
from service.sms import templates
from service.sms.templates import VAR_KEYS


def read_csv(path: pathlib.Path) -> list[dict]:
    """명단 파일을 읽습니다.

    Args:
        path: to·name·var1~var8 헤더를 가진 CSV

    Returns:
        list[dict]: 수신자 목록
    """
    # 한글 엑셀은 CSV 를 CP949 로 저장한다.
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"CSV 인코딩을 읽지 못했습니다(UTF-8·CP949 아님): {path}")

    with io.StringIO(text, newline="") as file:
        # restval="" 이라야 짧은 행의 to 가 None 이 되지 않고 검사 경로로
        # 들어온다. restkey 가 없으면 열이 밀린 행의 초과 필드가 조용히 버려진다.
        reader = csv.DictReader(file, restval="", restkey="__extra__")
        fields = reader.fieldnames or []
        # to 만 보면 열 이름이 틀렸을 때 치환값이 통째로 안 실린 채 발송된다.
        allowed = {"to", "name", *VAR_KEYS}
        unknown = [name for name in fields if name not in allowed]
        if "to" not in fields or unknown:
            raise ValueError(
                f"CSV 헤더가 맞지 않습니다: {fields}\n"
                f"기대: to,name,{','.join(VAR_KEYS)}"
            )
        rows = list(reader)
    overflow = [index for index, row in enumerate(rows, start=2) if "__extra__" in row]
    if overflow:
        raise ValueError(
            f"열 수가 헤더보다 많은 줄이 있습니다: {overflow}\n"
            "따옴표 없는 쉼표가 들어갔는지 확인하세요."
        )
    return rows


def show_history(phone: str) -> None:
    """그 번호에게 보낸 것을 출력합니다.

    Args:
        phone: 번호 (표기 무관)
    """
    rows = log.history(phone)
    if not rows:
        print(f"{phone} 에게 보낸 기록이 없습니다.")
        return
    for row in rows:
        if row["failed_at"]:
            stage, when = "실패", row["failed_at"]
        elif row["confirmed_at"]:
            stage, when = "도달", row["confirmed_at"]
        elif row["sent_at"]:
            stage, when = "발송", row["scheduled_for"] or row["sent_at"]
        else:
            stage, when = "모름", row["claimed_at"]
        print(f"{when:%Y-%m-%d %H:%M}  [{stage}] {row['campaign'] or 'CS'}")
        body = templates.render(row["content"], row["variables"])
        print(f"    {body.splitlines()[0][:60]}")


def main() -> None:
    """명령행 인자를 파싱해 발송합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="문자 발송")
    parser.add_argument("--campaign", help="발송 건 식별자. 중복 차단의 기준")
    parser.add_argument(
        "--cs", action="store_true", help="개인 CS. 중복 차단을 받지 않는다"
    )
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
    parser.add_argument("--history", metavar="번호", help="그 번호의 발송 기록")
    parser.add_argument("--dry-run", action="store_true", help="발송하지 않고 확인만")
    args = parser.parse_args()

    if args.history:
        show_history(args.history)
        return

    if args.cancel:
        # 발송 명령줄을 복사해 --cancel 만 붙이면 --dry-run 이 남는다.
        if args.dry_run:
            parser.error("--cancel 은 --dry-run 과 함께 쓸 수 없습니다.")
        print(sms_send.cancel_reserved(args.cancel))
        return

    if bool(args.campaign) == bool(args.cs):
        parser.error("--campaign 과 --cs 중 하나만 주세요.")

    if bool(args.csv) == bool(args.to):
        parser.error("--csv 와 --to 중 하나만 주세요.")

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
        campaign=None if args.cs else args.campaign,
        rows=rows,
        template_name=args.template,
        content=args.content,
        subject=args.subject,
        send_at=send_at,
        requested_by="script",
    )
    if result["sent"] == 0:
        print(
            f"대상 {result['requested']}명이 모두 이미 발송된 상태라 보내지 않았습니다."
        )
        blocked = log.pending(args.campaign) if args.campaign else []
        if blocked:
            print(
                f"그중 접수 여부를 모르는 {len(blocked)}건이 있습니다: "
                + ", ".join(row["phone"] for row in blocked)
                + "\n뿌리오 웹에서 접수 여부를 확인하세요."
            )
        return
    print(
        f"접수 완료 — 발송 {result['sent']}명"
        + (f" · 중복 제외 {result['skipped']}명" if result["skipped"] else "")
        + f"\nmessageKey {result['message_key']}"
        + (
            f"\n예약됨 {args.at} — 취소는 `--cancel {result['message_key']}`"
            if args.at
            else "\n접수 성공이지 도달 확인이 아닙니다."
        )
    )


if __name__ == "__main__":
    main()
