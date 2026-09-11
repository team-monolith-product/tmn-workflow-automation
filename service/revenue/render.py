"""
facts 를 에이전트가 읽을 글로 만든다.

JSON 을 통째로 주면 거래 600건이 컨텍스트를 먹는다. 집계는 표로, 거래는
걸러서 준다. 숫자는 전부 facts 에서 그대로 옮기고 여기서 계산하지 않는다 —
합계 한 줄만 예외인데 그것도 걸러진 행의 합이다.
"""

from collections import defaultdict
from typing import Any, Literal

Status = Literal["issued", "pipeline", "all"]

# 거래 표를 이 이상 주지 않는다. 더 필요하면 조건을 좁히라고 안내한다.
MAX_ROWS = 300


def _eok(value: int) -> str:
    return f"{value / 100_000_000:.2f}억"


def _latest_year(facts: dict[str, Any]) -> int:
    return max(int(year) for year in facts["aggregates"]["by_year"])


def _header(facts: dict[str, Any]) -> list[str]:
    meta = facts["meta"]
    problems = meta["problems"]
    health = (
        "없음"
        if not problems
        else f"{len(problems)}건 — revenue_health 로 확인. 경고가 있는 동안 숫자를 확정으로 쓰지 않는다"
    )
    return [
        f"기준: {meta['basis_label']} · {meta['amount_note']} · 분류규칙 {meta['taxonomy_version']}",
        f"매출장 읽은 시각 {meta['source_fetched_at']} · 검증 경고 {health}",
        "",
    ]


def render_summary(facts: dict[str, Any], year: int | None = None) -> str:
    """연도별 총계와 한 해의 분류·월별 집계."""
    aggregates = facts["aggregates"]
    meta = facts["meta"]
    year = year or _latest_year(facts)
    year_key = str(year)
    if year_key not in aggregates["by_year"]:
        years = ", ".join(sorted(aggregates["by_year"]))
        return f"{year}년 발행 매출이 없다. 있는 연도: {years}"

    lines = _header(facts)
    lines += [
        "## 연도별 총계 (발행 실적)",
        "",
        "| 연도 | 매출 | 건수 | 거래처 |",
        "|---|---:|---:|---:|",
    ]
    for y in sorted(aggregates["by_year"], reverse=True):
        txs = [tx for tx in facts["transactions"] if str(tx["year"]) == y]
        customers = len({tx["customer_key"] for tx in txs})
        total = aggregates["by_year"][y]
        lines.append(
            f"| {y} | {total:,}원 ({_eok(total)}) | {len(txs)}건 | {customers}곳 |"
        )
    lines.append("")

    default_view = meta["default_view"]
    view_meta = meta["views"][default_view]
    per_view = aggregates["by_view"][default_view].get(
        year_key, {"amount": {}, "count": {}}
    )
    year_total = aggregates["by_year"][year_key]
    order = [b for b in view_meta["order"] if b in per_view["amount"]]
    order += [b for b in per_view["amount"] if b not in order]

    lines += [
        f"## {year}년 {view_meta['label']}",
        "",
        "| 항목 | 금액 | 건수 | 비중 |",
        "|---|---:|---:|---:|",
    ]
    for bucket in order:
        amount = per_view["amount"][bucket]
        share = amount / year_total * 100 if year_total else 0
        lines.append(
            f"| {bucket} | {amount:,} | {per_view['count'][bucket]}건 | {share:.1f}% |"
        )
    lines.append("")

    child = view_meta.get("child")
    if child and child in meta["views"]:
        empty_label = meta["views"][child].get("empty", "")
        kids: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for tx in facts["transactions"]:
            if str(tx["year"]) == year_key:
                kids[tx["views"][default_view]][tx["views"][child]] += tx["amount"]
        rows = []
        master = meta.get("taxonomy_master") or {}
        for bucket in order:
            subs = kids.get(bucket, {})
            if not subs or (len(subs) == 1 and empty_label in subs):
                continue
            seq = [s for s in (master.get(bucket) or []) if s in subs]
            seq += [s for s in subs if s not in seq]
            rows += [f"| {bucket} | {sub} | {subs[sub]:,} |" for sub in seq]
        if rows:
            lines += [f"## {year}년 {meta['views'][child]['label']}", ""]
            lines += ["| 대분류 | 세부분류 | 금액 |", "|---|---|---:|"] + rows
            lines.append("")

    months = sorted(m for m in aggregates["by_month"] if m.startswith(year_key))
    if months:
        lines += [f"## {year}년 월별 (발행일 기준)", "", "| 월 | 금액 |", "|---|---:|"]
        lines += [f"| {m} | {aggregates['by_month'][m]:,} |" for m in months]
        lines.append("")

    pipeline = facts["pipeline"]
    if pipeline["count"]:
        lines += [
            "## 파이프라인 (매출 아님)",
            "",
            f"매출장 「예정」 행 {pipeline['count']}건 {pipeline['total']:,}원 ({_eok(pipeline['total'])})."
            " 계약은 됐으나 세금계산서가 나가지 않은 금액이라 위 총계에 들어 있지 않다."
            " 더하려면 「확정매출(발행+예정)」이라고 이름을 붙여 구분한다.",
            "",
        ]
    lines.append("거래 단위는 revenue_transactions 로 본다.")
    return "\n".join(lines)


def _matches(tx: dict[str, Any], category: str | None, customer: str | None) -> bool:
    if category:
        haystack = [
            tx["category_raw"],
            tx.get("subcategory_raw", ""),
            *tx["views"].values(),
        ]
        if not any(category in value for value in haystack if value):
            return False
    if customer:
        needle = customer.casefold()
        if (
            needle not in tx["counterparty"].casefold()
            and needle not in tx["customer_key"].casefold()
        ):
            return False
    return True


def render_transactions(
    facts: dict[str, Any],
    year: int | None = None,
    category: str | None = None,
    customer: str | None = None,
    status: Status = "issued",
    limit: int = 100,
) -> str:
    """조건에 맞는 거래를 표로. 금액 큰 순."""
    pool: list[dict[str, Any]] = []
    if status in ("issued", "all"):
        pool += facts["transactions"]
    if status in ("pipeline", "all"):
        pool += facts["pipeline"]["transactions"]

    rows = [
        tx
        for tx in pool
        if (year is None or tx["year"] == year) and _matches(tx, category, customer)
    ]
    rows.sort(key=lambda tx: -tx["amount"])
    limit = max(1, min(limit, MAX_ROWS))

    conditions = [
        f"연도 {year}" if year else "전 연도",
        f"분류 «{category}»" if category else None,
        f"거래처 «{customer}»" if customer else None,
        {"issued": "발행분", "pipeline": "예정분", "all": "발행+예정"}[status],
    ]
    total = sum(tx["amount"] for tx in rows)
    lines = _header(facts)
    lines.append(
        f"조건: {' · '.join(c for c in conditions if c)} → {len(rows)}건 {total:,}원 ({_eok(total)})"
    )
    if len(rows) > limit:
        lines.append(
            f"상위 {limit}건만 보인다. 조건을 좁히거나 limit 을 키운다(최대 {MAX_ROWS})."
        )
    if not rows:
        return "\n".join(lines)

    default_view = facts["meta"]["default_view"]
    lines += [
        "",
        "| 상태 | 발행일 | 귀속 | 거래처 | 품목 | 수량 | 단가 | 공급가액 | 대분류 | 세부분류 | 비고 |",
        "|---|---|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for tx in rows[:limit]:
        qty = f"{tx['qty']:,}" if tx["qty"] else ""
        unit = f"{tx['unit_price']:,}" if tx["unit_price"] else ""
        lines.append(
            f"| {tx['status']} | {tx.get('date') or '예정'} | {tx['year']} | {tx['counterparty']}"
            f" | {tx['item']} | {qty} | {unit} | {tx['amount']:,} | {tx['views'][default_view]}"
            f" | {tx.get('subcategory_raw', '')} | {tx['note']} |"
        )
    return "\n".join(lines)


def render_health(facts: dict[str, Any]) -> str:
    """검증 경고 목록. 비어 있으면 숫자를 믿어도 된다."""
    meta = facts["meta"]
    lines = [
        f"facts 생성 {meta['generated_at']} · 매출장 읽은 시각 {meta['source_fetched_at']}",
        f"분류규칙 {meta['taxonomy_version']} · 기본 뷰 {meta['default_view']}",
        f"발행 {len(facts['transactions'])}건 · 예정 {facts['pipeline']['count']}건",
        "",
    ]
    if not meta["problems"]:
        lines.append(
            "검증 경고 없음. 연도 총계가 기준값과 맞고 미분류·분류마스터 위반이 없다."
        )
        return "\n".join(lines)
    lines.append(
        f"검증 경고 {len(meta['problems'])}건 — 해소 전까지 숫자를 확정으로 쓰지 않는다."
    )
    lines += [f"- {problem}" for problem in meta["problems"]]
    return "\n".join(lines)
