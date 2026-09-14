import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dotenv import load_dotenv
import sentry_sdk
from slack_sdk import WebClient

from service.db import connect, fetch_all, fetch_one
from service.revenue.enrich import enrich_rows, fetch_crm
from service.revenue.normalize import fetch_rows

CHANNEL_ID = "C0BA4UXD2G7"
KST = timezone(timedelta(hours=9))
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
TOTALS = """
SELECT year,
       coalesce(sum(amount) FILTER (WHERE status='issued'), 0) AS issued,
       count(*) FILTER (WHERE status='issued') AS issued_count,
       coalesce(sum(amount) FILTER (WHERE status='planned'), 0) AS planned,
       count(*) FILTER (WHERE status='planned') AS planned_count
FROM revenue_transactions GROUP BY year
"""


def collect_rows() -> tuple[list[dict], list[str]]:
    rows, warnings = fetch_rows()
    orgs, deals, programs = asyncio.run(fetch_crm())
    warnings += enrich_rows(rows, orgs, deals, programs)
    return rows, warnings


def replace_rows(conn, rows: list[dict]) -> None:
    if not rows:
        raise ValueError("빈 매출장으로 DB를 교체하지 않습니다")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM revenue_transactions")
        cur.executemany(
            INSERT, [tuple(row.get(column) for column in COLUMNS) for row in rows]
        )


def year_totals(conn) -> dict[int, dict]:
    return {row["year"]: row for row in fetch_all(conn, TOTALS)}


def won(amount: Decimal | int) -> str:
    return f"{int(amount):,}원"


def eok(amount: Decimal | int) -> str:
    return f"{Decimal(amount) / 100_000_000:.2f}억"


def delta(after: Decimal | int, before: Decimal | int, count: int) -> str:
    change = Decimal(after) - Decimal(before)
    if change == 0 and count == 0:
        return "변동 없음"
    return f"{'+' if change >= 0 else '-'}{won(abs(change))} · {count:+d}건"


def format_report(
    before: dict[int, dict],
    after: dict[int, dict],
    synced_at: datetime,
    warnings: list[str],
) -> str:
    years = sorted(set(before) | set(after), reverse=True)
    issued_count = sum(row["issued_count"] for row in after.values())
    planned_count = sum(row["planned_count"] for row in after.values())
    lines = [
        f"매출장 반영 {synced_at.astimezone(KST):%m-%d %H:%M} · 발행 {issued_count}건 · 예정 {planned_count}건"
    ]
    empty = {"issued": 0, "issued_count": 0, "planned": 0, "planned_count": 0}
    for year in years:
        old, new = before.get(year, empty), after.get(year, empty)
        issued = delta(
            new["issued"], old["issued"], new["issued_count"] - old["issued_count"]
        )
        planned = delta(
            new["planned"], old["planned"], new["planned_count"] - old["planned_count"]
        )
        if issued == planned == "변동 없음" and year != years[0]:
            continue
        lines.append(
            f"{year} 발행 {eok(new['issued'])} ({issued}) · 예정 {eok(new['planned'])} ({planned})"
        )
    if warnings:
        lines.append(f":warning: 정규화 경고 {len(warnings)}건 — Sentry 참고")
        lines += [f"• {warning}" for warning in warnings[:5]]
    return "\n".join(lines)


def notify(text: str) -> None:
    WebClient(token=os.environ["SLACK_BOT_TOKEN"]).chat_postMessage(
        channel=CHANNEL_ID, text=text
    )


def main(dry_run: bool = False) -> None:
    load_dotenv()
    if dry_run:
        rows, warnings = collect_rows()
        print(f"[revenue] dry-run: {len(rows)}행")
        for warning in warnings:
            print(f"[revenue] 경고: {warning}")
        return
    try:
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7243)")
            before = year_totals(conn)
            rows, warnings = collect_rows()
            replace_rows(conn, rows)
            after = year_totals(conn)
            synced_at = fetch_one(
                conn, "SELECT max(synced_at) AS synced_at FROM revenue_transactions"
            )["synced_at"]
    except Exception as error:
        notify(
            ":x: 매출장 동기화 실패 — 대시보드는 이전 데이터 그대로입니다.\n"
            f"{type(error).__name__}: {error}"
        )
        raise
    print(f"[revenue] 동기화 완료: {len(rows)}행")
    for warning in warnings:
        print(f"[revenue] 경고: {warning}")
    if warnings:
        sentry_sdk.capture_message(
            "매출장 정규화 오류\n" + "\n".join(warnings), level="error"
        )
    notify(format_report(before, after, synced_at, warnings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    main(dry_run=parser.parse_args().dry_run)
