"""
매출 보강 테이블(knowledge/revenue/enrichment.yml) 갱신.

매출장 거래처를 축으로 노션 CRM(Athena 미러 notion_prd)에서 학교급·교육청·예산출처·
사업 마스터를 끌어온다. 스케줄에 올리지 않는다 — 결과는 사람이 diff 를 보고 커밋한다.
주 1회 또는 필요할 때 손으로 돌린다.

  export REDASH_BASE_URL=https://redash.codle.io
  export REDASH_API_KEY=...
  python scripts/enrich_revenue_customers.py            # 파일을 덮어쓴다
  python scripts/enrich_revenue_customers.py --dry-run  # 커버리지만 출력
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import asyncio

import yaml
from dotenv import load_dotenv

from service.revenue.config import KNOWLEDGE_DIR, load_enrichment, load_sources
from service.revenue.enrich import (
    SQL_DEALS,
    SQL_ORGS,
    SQL_PROGRAMS,
    build_enrichment,
    ledger_counterparties,
    run_sql,
)
from service.revenue.ledger import fetch_ledger

load_dotenv()


async def main(dry_run: bool = False) -> None:
    sources = load_sources()
    raw = await asyncio.to_thread(fetch_ledger, sources)
    ledger = ledger_counterparties(raw, sources)
    print(f"매출장 거래처 {len(ledger)}곳")

    print("학교 마스터 인출 중...")
    orgs = await run_sql(SQL_ORGS)
    print(f"  {len(orgs)}개교")
    print("딜 집계 인출 중...")
    deals = await run_sql(SQL_DEALS)
    print(f"  {len(deals)}개교에 딜 있음")
    print("사업 마스터 인출 중...")
    programs = await run_sql(SQL_PROGRAMS)
    print(f"  {len(programs)}건")

    out = build_enrichment(ledger, orgs, deals, programs, load_enrichment(sources))
    coverage = out["coverage"]
    print(
        f"보강된 거래처 {coverage['matched']} / 전체 {coverage['ledger_counterparties']}\n"
        f"  CRM 미러에 없어 학교급만 도출 {coverage['level_derived_from_name']}곳 (조사 큐)\n"
        f"  학교 분류인데 학교가 아닌 거래처 {coverage['non_school_in_school_category']}곳"
        f" · 사업 {len(out['programs'])} · 수기 {len(out['manual'])}"
    )
    if coverage["alias_targets_missing_from_master"]:
        print(
            "  ! 별칭이 가리키는데 사업 마스터에 없는 이름:"
            f" {coverage['alias_targets_missing_from_master']}"
        )
    if dry_run:
        print("[dry-run] 파일에 쓰지 않았다.")
        return

    path = KNOWLEDGE_DIR / Path(sources["enrichment"]["file"]).name
    path.write_text(
        yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=200),
        encoding="utf-8",
    )
    print(f"기록 완료 {path}. diff 를 보고 커밋한다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="파일에 쓰지 않고 커버리지만 출력"
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
