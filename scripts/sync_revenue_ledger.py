"""매출장·CRM을 정규화해 DB 캐시를 교체한다. --dry-run은 읽기와 검증만 한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import os

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import load_config
from service.db import connect
from service.revenue.config import load_rules, load_sources
from service.revenue.enrich import enrich_rows, fetch_crm
from service.revenue.ledger import fetch_ledger
from service.revenue.normalize import normalize_rows

COLUMNS = (
    "year",
    "issued_on",
    "status",
    "customer",
    "item",
    "quantity",
    "unit_price",
    "amount",
    "tax",
    "total",
    "category",
    "subcategory",
    "note",
    "school_level",
    "edu_office",
    "budget_sources",
    "terms",
    "program_name",
    "program_client",
    "program_budget",
    "program_our_revenue",
    "program_stage",
)
INSERT = f"INSERT INTO revenue_transactions ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})"


def collect_rows() -> tuple[list[dict], list[str]]:
    """외부 조회·정규화·보강을 한 배치에서 끝낸다."""
    sources, rules = load_sources(), load_rules()
    rows, warnings = normalize_rows(fetch_ledger(sources), sources, rules)
    orgs, deals, programs = asyncio.run(fetch_crm())
    warnings += enrich_rows(rows, orgs, deals, programs, rules)
    return rows, warnings


def replace_rows(conn, rows: list[dict]) -> None:
    """호출자의 트랜잭션 안에서 전체를 교체한다. 같은 내용의 원본 행도 보존한다."""
    if not rows:
        raise ValueError("빈 매출장으로 DB를 교체하지 않습니다")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM revenue_transactions")
        cur.executemany(
            INSERT, [tuple(row.get(column) for column in COLUMNS) for row in rows]
        )


def main(dry_run: bool = False) -> None:
    """실패하면 기존 DB가 남고 예외는 스케줄러/Sentry에 전달된다."""
    load_dotenv()
    if dry_run:
        rows, warnings = collect_rows()
    else:
        with connect() as conn:
            # 수동 실행과 스케줄 실행이 겹쳐 오래된 결과를 나중에 쓰지 않게 한다.
            conn.execute("SELECT pg_advisory_xact_lock(7243)")
            rows, warnings = collect_rows()
            replace_rows(conn, rows)
    print(f"[revenue] {'dry-run' if dry_run else '동기화 완료'}: {len(rows)}행")
    for warning in warnings:
        print(f"[revenue] 경고: {warning}")
    config = load_config().revenue
    if warnings and not dry_run and config:
        text = f"매출장 검증 경고 {len(warnings)}건\n" + "\n".join(warnings[:15])
        if len(warnings) > 15:
            text += f"\n외 {len(warnings) - 15}건: 배치 로그 확인"
        WebClient(token=os.environ["SLACK_BOT_TOKEN"]).chat_postMessage(
            channel=config.alert_channel_id, text=text
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    main(dry_run=parser.parse_args().dry_run)
