import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio

from dotenv import load_dotenv
import sentry_sdk

from service.db import connect
from service.revenue.config import load_rules, load_sources
from service.revenue.enrich import enrich_rows, fetch_crm
from service.revenue.normalize import fetch_rows

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
    sources, rules = load_sources(), load_rules()
    rows, warnings = fetch_rows(sources, rules)
    orgs, deals, programs = asyncio.run(fetch_crm())
    warnings += enrich_rows(rows, orgs, deals, programs, rules)
    return rows, warnings


def replace_rows(conn, rows: list[dict]) -> None:
    if not rows:
        raise ValueError("빈 매출장으로 DB를 교체하지 않습니다")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM revenue_transactions")
        cur.executemany(
            INSERT, [tuple(row.get(column) for column in COLUMNS) for row in rows]
        )


def main(dry_run: bool = False) -> None:
    load_dotenv()
    if dry_run:
        rows, warnings = collect_rows()
    else:
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7243)")
            rows, warnings = collect_rows()
            replace_rows(conn, rows)
    print(f"[revenue] {'dry-run' if dry_run else '동기화 완료'}: {len(rows)}행")
    for warning in warnings:
        print(f"[revenue] 경고: {warning}")
    if warnings and not dry_run:
        sentry_sdk.capture_message(
            "매출장 정규화 오류\n" + "\n".join(warnings), level="error"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    main(dry_run=parser.parse_args().dry_run)
