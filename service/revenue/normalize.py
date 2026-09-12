import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from service.revenue.taxonomy import (
    UNCLASSIFIED,
    classify,
    clean,
    parse_taxonomy_master,
)


def number(value: Any, required: bool = False) -> Decimal | None:
    text = clean(value).replace(",", "").replace("₩", "")
    if not text and not required:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"숫자를 읽을 수 없음: {value!r}") from error
    if not result.is_finite():
        raise ValueError(f"유한수가 아님: {value!r}")
    return result


def normalize_rows(
    raw: dict, sources: dict, rules: dict
) -> tuple[list[dict], list[str]]:
    master = parse_taxonomy_master(raw, sources)
    if not master:
        raise ValueError("매출장 분류마스터가 비어 있습니다")
    product = rules["product"]
    warnings = []
    rows = []
    totals = defaultdict(Decimal)
    for tab, blob in raw["tabs"].items():
        columns = {**sources["ledger"]["columns"], **blob.get("columns", {})}
        for row_number, cells in enumerate(blob["rows"][1:], start=2):

            def cell(name):
                index = columns.get(name, -1)
                return cells[index] if 0 <= index < len(cells) else ""

            if not any(clean(cell(k)) for k in ("date", "counterparty", "item")):
                if all(
                    number(cell(k)) in (None, 0) for k in ("amount", "tax", "total")
                ):
                    continue
            try:
                date_text = clean(cell("date"))
                status = (
                    "planned" if date_text in sources["planned_markers"] else "issued"
                )
                issued_on = None
                if status == "issued":
                    match = re.fullmatch(
                        r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", date_text
                    )
                    if not match:
                        raise ValueError(f"발행일을 읽을 수 없음: {date_text!r}")
                    issued_on = date(*(int(v) for v in match.groups()))
                row = {
                    "year": blob["fiscal_year"],
                    "issued_on": issued_on,
                    "status": status,
                    "customer": clean(cell("counterparty")),
                    "item": clean(cell("item")),
                    "quantity": number(cell("qty")),
                    "unit_price": number(cell("unit_price")),
                    "amount": number(cell("amount"), required=True),
                    "tax": number(cell("tax")),
                    "total": number(cell("total")),
                    "subcategory": clean(cell("subcategory_raw")) or None,
                    "note": clean(cell("note")) or None,
                }
            except ValueError as error:
                raise ValueError(f"{tab} {row_number}행: {error}") from error
            original_category = clean(cell("category_raw"))
            row["category"] = classify(
                {
                    "year": row["year"],
                    "counterparty": row["customer"],
                    "item": row["item"],
                    "amount": row["amount"],
                    "category_raw": original_category,
                    "subcategory_raw": row["subcategory"],
                },
                product,
                master,
            )
            if row["category"] == UNCLASSIFIED:
                warnings.append(f"{tab} {row_number}행: 미분류 «{original_category}»")
            if row["subcategory"] and row["subcategory"] not in master.get(
                original_category, []
            ):
                warnings.append(
                    f"{tab} {row_number}행: 분류마스터에 없는 조합 «{original_category} / {row['subcategory']}»"
                )
            if status == "issued":
                totals[row["year"]] += row["amount"]
            rows.append(row)
    if not rows:
        raise ValueError("매출장 거래가 비어 있습니다. 기존 DB를 유지합니다")
    assertions = rules.get("assertions", {})
    for year, expected in assertions.get("year_totals_issued", {}).items():
        if abs(totals[int(year)] - Decimal(str(expected))) > assertions.get(
            "tolerance_won", 5
        ):
            warnings.append(
                f"연도 총계 불일치 {year}: 계산 {totals[int(year)]:,} / 기준 {expected:,}"
            )
    targets = set(product["base"].values()) | {r["then"] for r in product["exceptions"]}
    for target in sorted(targets - master.keys()):
        warnings.append(f"분류 규칙의 «{target}»가 분류마스터에 없습니다")
    return rows, warnings
