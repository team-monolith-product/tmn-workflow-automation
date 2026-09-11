"""
매출장 검증. 매일 한 번 매출장을 읽어 facts 를 만들고, 검증 경고가 있으면 슬랙에 알린다.

검증하는 것:
  - 연도별 발행 총계가 taxonomy.yml 의 assertions(SSOT)와 맞는가
  - 규칙에 안 걸려 「미분류」로 떨어진 거래가 있는가
  - 매출장 행이 「분류마스터」 탭이 정한 (대분류, 세부분류) 조합을 벗어났는가
  - 규칙이 가리키는 대분류가 분류마스터에 실재하는가
  - 날짜를 못 읽은 행, 사업 마스터에 없는 사업 별칭

경고가 없으면 아무것도 보내지 않는다. 매일 「이상 없음」이 오면 곧 읽지 않게 된다.
시트를 못 읽으면(공유 해제·탭 이름 변경) 예외가 그대로 나가 스케줄러 로그와 Sentry 에 잡힌다.

  python scripts/validate_revenue_ledger.py --dry-run
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import load_config
from service.revenue.facts import build_fresh

load_dotenv()

# 슬랙 메시지 하나에 담는 경고 수. 그 이상은 revenue_health 로 보라고 한다.
MAX_PROBLEMS_IN_MESSAGE = 15


def format_alert(facts: dict) -> str:
    """검증 경고를 슬랙 메시지로."""
    meta = facts["meta"]
    problems = meta["problems"]
    lines = [
        f":warning: 매출장 검증 경고 {len(problems)}건 — 해소 전까지 이 숫자를 확정으로 쓰지 않는다",
        f"매출장 읽은 시각 {meta['source_fetched_at']} · 분류규칙 {meta['taxonomy_version']}",
        "",
    ]
    lines += [f"• {problem}" for problem in problems[:MAX_PROBLEMS_IN_MESSAGE]]
    if len(problems) > MAX_PROBLEMS_IN_MESSAGE:
        lines.append(
            f"… 외 {len(problems) - MAX_PROBLEMS_IN_MESSAGE}건. MCP revenue_health 로 전체를 본다"
        )
    return "\n".join(lines)


def main(dry_run: bool = False) -> None:
    config = load_config().revenue
    if config is None:
        print("revenue 설정이 config.yaml 에 없습니다.")
        return

    facts = build_fresh()
    aggregates = facts["aggregates"]
    for year in sorted(aggregates["by_year"], reverse=True):
        print(f"[revenue] {year}: {aggregates['by_year'][year]:,}원")
    print(
        f"[revenue] 발행 {len(facts['transactions'])}건 · 예정 {facts['pipeline']['count']}건"
        f" {facts['pipeline']['total']:,}원"
    )

    problems = facts["meta"]["problems"]
    if not problems:
        print("[revenue] 검증 경고 없음. Slack 전송 생략.")
        return

    text = format_alert(facts)
    if dry_run:
        print("----- DRY RUN: Slack 메시지 -----")
        print(text)
        return

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    client.chat_postMessage(channel=config.alert_channel_id, text=text)
    print(
        f"[revenue] 경고 {len(problems)}건 Slack 전송 완료 → {config.alert_channel_id}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack 메시지를 보내지 않고 콘솔에 출력만",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
