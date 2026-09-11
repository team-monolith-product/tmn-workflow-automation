"""
raw 스냅샷 + 분류규칙 + 보강테이블 -> facts.

원칙:
  - 인식기준은 sources.yml 의 basis. 현재 issued = 세금계산서 발행 실적만.
  - 예정분은 매출에서 빼고 pipeline 으로 따로 낸다. 두 숫자를 섞지 않는다.
  - 분류가 규칙에 안 걸리면 「미분류」로 남기고 경고한다. 임의 배정하지 않는다.
  - assertions 와 어긋나면 경고를 facts 에 적는다. 조용히 넘어가지 않는다.

facts 구조:
  meta          생성시각, 인식기준, 분류규칙 버전, 뷰 정의, 분류마스터, 검증 경고(problems)
  customers     거래처 보강 조회표 {customer_key: {...}}
  programs      정부·공공 사업 조회표 {사업명: {...}}
  transactions  발행분 거래 1건 = 1 dict
  pipeline      예정분. total, count, transactions
  aggregates    by_year, by_month, by_view, by_customer
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from service.revenue.taxonomy import (
    UNCLASSIFIED,
    apply_master,
    classify,
    clean,
    parse_taxonomy_master,
    validate_against_master,
    validate_rules_against_master,
)

KST = timezone(timedelta(hours=9))
BASIS_LABEL = "세금계산서 발행 실적 기준 (예정분 제외)"
AMOUNT_NOTE = "모든 금액은 공급가액(부가세 제외), 원 단위"

CORP_NOISE = re.compile(
    r"주식회사|㈜|\(주\)|\(재\)|재단법인|사단법인|\(사\)|유한회사|\(유\)|법인"
)
DATE_PATTERN = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")


def won(value: Any) -> int:
    """셀 값을 원 단위 정수로. 빈 칸·문자는 0."""
    text = str(value).replace(",", "").replace("₩", "").strip()
    if not text:
        return 0
    try:
        return int(round(float(text)))
    except ValueError:
        return 0


def customer_key(name: str) -> str:
    """거래처명에서 법인 접두어와 공백을 걷어낸 조회 키."""
    return re.sub(r"\s+", "", CORP_NOISE.sub("", name)).strip() or name


# ── 정규화


def normalize(
    raw: dict[str, Any], sources: dict[str, Any]
) -> tuple[list[dict], list[dict], list[str]]:
    """raw 행 -> 거래 dict. 발행분과 예정분을 가른다.

    Returns:
        tuple: (발행분, 예정분, 경고). 날짜를 못 읽은 행은 발행분에서 빼고 경고에 남긴다
    """
    base_cols = sources["ledger"]["columns"]
    markers = set(sources["basis"]["pipeline_markers"])
    issued: list[dict] = []
    pipeline: list[dict] = []
    warnings: list[str] = []

    def cell(row: list, cols: dict[str, int], name: str) -> Any:
        index = cols.get(name, -1)
        return row[index] if 0 <= index < len(row) else ""

    for tab, blob in raw["tabs"].items():
        fiscal_year = blob["fiscal_year"]
        # 탭마다 열 배치가 다를 수 있다. 2026(신)은 세부분류가 끼어들어 비고가 밀렸다.
        cols = {**base_cols, **(blob.get("columns") or {})}
        width = max(max(cols.values()) + 1, 12)
        for row_number, row in enumerate(blob["rows"][1:], start=2):
            row = (list(row) + [""] * width)[:width]
            date_cell = clean(cell(row, cols, "date"))
            counterparty = clean(cell(row, cols, "counterparty"))
            item = clean(cell(row, cols, "item"))
            amount = won(cell(row, cols, "amount"))

            if not date_cell and not counterparty and amount == 0:
                continue
            if amount == 0 and not counterparty and not item:
                continue

            tx = {
                "id": f"{fiscal_year}-{row_number}",
                "sheet_tab": tab,
                "sheet_row": row_number,
                "fiscal_year": fiscal_year,
                "counterparty": counterparty,
                "customer_key": customer_key(counterparty),
                "item": item,
                "qty": won(cell(row, cols, "qty")),
                "unit_price": won(cell(row, cols, "unit_price")),
                "amount": amount,
                "tax": won(cell(row, cols, "tax")),
                "total": won(cell(row, cols, "total")),
                "category_raw": clean(cell(row, cols, "category_raw")) or UNCLASSIFIED,
                "subcategory_raw": clean(cell(row, cols, "subcategory_raw")),
                "note": clean(cell(row, cols, "note")),
            }

            if date_cell in markers:
                tx.update(date=None, year=fiscal_year, month=None, status="pipeline")
                pipeline.append(tx)
                continue

            match = DATE_PATTERN.match(date_cell)
            if not match:
                if date_cell:
                    warnings.append(
                        f"{tab} {row_number}행: 날짜를 못 읽었다 «{date_cell}» — 발행분에서 제외했다"
                    )
                continue
            year, month, day = (int(group) for group in match.groups())
            tx["date"] = f"{year:04d}-{month:02d}-{day:02d}"
            # 귀속연도는 탭 기준. 발행일 연도와 다를 수 있다(이연분)
            tx["year"] = fiscal_year
            tx["issue_year"] = year
            tx["month"] = f"{year:04d}-{month:02d}"
            tx["status"] = "issued"
            if year != fiscal_year:
                tx["note"] = (tx["note"] + " / 발행연도와 귀속연도 불일치").strip(" /")
            issued.append(tx)

    return issued, pipeline, warnings


# ── 집계


def aggregate(txs: list[dict], views: dict[str, Any]) -> dict[str, Any]:
    """연도·월·뷰·거래처별 합계."""
    out: dict[str, Any] = {
        "by_year": {},
        "by_month": {},
        "by_view": {},
        "by_customer": {},
    }

    for tx in txs:
        year = str(tx["year"])
        out["by_year"][year] = out["by_year"].get(year, 0) + tx["amount"]
        if tx.get("month"):
            out["by_month"][tx["month"]] = (
                out["by_month"].get(tx["month"], 0) + tx["amount"]
            )

    for view_name in views:
        amounts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for tx in txs:
            bucket = tx["views"][view_name]
            amounts[str(tx["year"])][bucket] += tx["amount"]
            counts[str(tx["year"])][bucket] += 1
        out["by_view"][view_name] = {
            year: {"amount": dict(buckets), "count": dict(counts[year])}
            for year, buckets in amounts.items()
        }

    customers: dict[str, dict] = defaultdict(
        lambda: {"amount": 0, "count": 0, "years": set(), "name": ""}
    )
    for tx in txs:
        entry = customers[tx["customer_key"]]
        entry["amount"] += tx["amount"]
        entry["count"] += 1
        entry["years"].add(tx["year"])
        entry["name"] = entry["name"] or tx["counterparty"]
    out["by_customer"] = {
        key: {**value, "years": sorted(value["years"])}
        for key, value in sorted(customers.items(), key=lambda kv: -kv[1]["amount"])
    }
    return out


# ── 검증


def validate(
    issued: list[dict], taxonomy: dict[str, Any], views: dict[str, Any]
) -> list[str]:
    """연도 총계가 SSOT 와 맞는지, 미분류가 남았는지."""
    problems: list[str] = []

    totals: dict[int, int] = defaultdict(int)
    for tx in issued:
        totals[tx["year"]] += tx["amount"]

    assertions = taxonomy.get("assertions") or {}
    tolerance = assertions.get("tolerance_won", 5)
    for year, expected in (assertions.get("year_totals_issued") or {}).items():
        got = totals.get(int(year), 0)
        if abs(got - expected) > tolerance:
            problems.append(
                f"연도 총계 불일치 {year}: 계산 {got:,} vs 기준 {expected:,}"
                f" (차 {got - expected:+,}). 매출장이 바뀌었거나 taxonomy.yml 의 assertions 가 낡았다"
            )

    for view_name in views:
        unmatched: dict[str, int] = defaultdict(int)
        for tx in issued:
            if tx["views"][view_name] == UNCLASSIFIED:
                unmatched[tx["category_raw"]] += tx["amount"]
        for raw_category, amount in unmatched.items():
            problems.append(
                f"[{view_name}] 원분류 «{raw_category}» {amount:,}원이 규칙에 안 걸려 미분류다."
                f" taxonomy.yml 의 views.{view_name}.base 에 추가한다"
            )
    return problems


# ── 조립


def build_facts(
    raw: dict[str, Any],
    sources: dict[str, Any],
    taxonomy: dict[str, Any],
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    """raw 스냅샷을 facts 로 만든다. 외부 호출이 없는 순수 함수다.

    Args:
        raw: ledger.fetch_ledger 결과
        sources: sources.yml
        taxonomy: taxonomy.yml. views 의 order·prefer_raw 를 분류마스터로 덮어쓴다
        enrichment: enrichment.yml. 없으면 {}

    Returns:
        dict: facts. meta.problems 가 비어 있지 않으면 그 숫자를 확정으로 쓰지 않는다
    """
    customers = enrichment.get("customers") or {}
    manual = enrichment.get("manual") or {}
    programs = enrichment.get("programs") or {}
    aliases = enrichment.get("program_aliases") or {}
    # 긴 별칭부터 맞춰야 「AI선도교사 1권역」이 「선도교사」보다 먼저 걸린다.
    alias_order = sorted(aliases.items(), key=lambda kv: -len(kv[0]))

    views = taxonomy["views"]
    issued, pipeline_txs, warnings = normalize(raw, sources)
    used_programs: set[str] = set()
    used_customers: dict[str, dict] = {}

    master = parse_taxonomy_master(raw, sources)
    apply_master(views, master)

    for tx in issued + pipeline_txs:
        info = {
            **(customers.get(tx["customer_key"]) or {}),
            **(manual.get(tx["customer_key"]) or {}),
        }
        if info:
            tx["enrich"] = info
            used_customers[tx["customer_key"]] = info

        haystack = f"{tx['counterparty']} {tx['item']}"
        for alias, target in alias_order:
            if alias in haystack:
                tx["program_key"] = target
                used_programs.add(target)
                break

        tx["views"] = {}
        tx["rule"] = {}
        for view_name, view in views.items():
            bucket, rule_id = classify(tx, view)
            tx["views"][view_name] = bucket
            if rule_id:
                tx["rule"][view_name] = rule_id

    for tx in issued + pipeline_txs:
        tx.pop("enrich", None)  # 본문은 facts.customers 조회표에 한 번만 둔다

    problems = warnings + validate(issued, taxonomy, views)
    problems += validate_against_master(issued + pipeline_txs, master)
    problems += validate_rules_against_master(views, master)
    for missing in sorted(
        program for program in used_programs if program not in programs
    ):
        problems.append(
            f"사업 별칭이 «{missing}»를 가리키는데 사업 마스터에 그 이름이 없다."
            " enrichment.yml 의 program_aliases 를 고치거나 CRM 에 사업을 등록한다"
        )

    aggregates = aggregate(issued, views)
    return {
        "meta": {
            "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "source_fetched_at": raw["fetched_at"],
            "source_spreadsheet_id": raw["spreadsheet_id"],
            "basis": sources["basis"]["mode"],
            "basis_label": BASIS_LABEL,
            "amount_note": AMOUNT_NOTE,
            "taxonomy_version": taxonomy["version"],
            "default_view": taxonomy["default_view"],
            "taxonomy_master": master,
            "views": {
                name: {
                    "label": view["label"],
                    "note": view.get("note", ""),
                    "order": view.get("order", []),
                    "kind": view.get("kind", "rules"),
                    "empty": view.get("empty", ""),
                    "child": view.get("child", ""),
                }
                for name, view in views.items()
            },
            "problems": problems,
        },
        "customers": used_customers,
        "programs": {
            name: programs[name] for name in programs if name in used_programs
        },
        "transactions": issued,
        "pipeline": {
            "total": sum(tx["amount"] for tx in pipeline_txs),
            "count": len(pipeline_txs),
            "transactions": pipeline_txs,
        },
        "aggregates": aggregates,
    }
