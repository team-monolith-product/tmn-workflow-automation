import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from gspread.utils import absolute_range_name

from api.google_sheets import DEFAULT_ACCOUNT, get_spreadsheet_values_batch

UNCLASSIFIED = "미분류"


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def fetch_rows(
    sources: dict, rules: dict, account: str = DEFAULT_ACCOUNT
) -> tuple[list[dict], list[str]]:
    ledger = sources["ledger"]
    master = ledger["taxonomy_tab"]
    ranges = [absolute_range_name(tab, ledger["range"]) for tab in ledger["tabs"]]
    ranges.append(absolute_range_name(master["name"], master["range"]))
    response = get_spreadsheet_values_batch(
        ledger["spreadsheet_id"],
        ranges,
        account=account,
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="FORMATTED_STRING",
    )
    return normalize_rows(response, sources, rules)


def parse_taxonomy_master(rows: list[list], spec: dict) -> dict[str, list[str]]:
    if not rows or not spec:
        return {}

    def at(row_index: int, col_index: int) -> str:
        row = rows[row_index] if 0 <= row_index < len(rows) else []
        return clean(row[col_index]) if 0 <= col_index < len(row) else ""

    majors = []
    for row_index in range(spec["major_start_row"] - 1, len(rows)):
        value = at(row_index, spec["major_col"])
        if value:
            majors.append(value)

    details: dict[str, list[str]] = {major: [] for major in majors}
    header_row = spec["detail_header_row"] - 1
    for col_index in range(spec["detail_first_col"], spec["detail_last_col"] + 1):
        major = at(header_row, col_index)
        if not major:
            continue
        details[major] = [
            at(row_index, col_index)
            for row_index in range(spec["detail_start_row"] - 1, len(rows))
            if at(row_index, col_index)
        ]

    return details


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
    response: dict, sources: dict, rules: dict
) -> tuple[list[dict], list[str]]:
    ledger = sources["ledger"]
    values = response["valueRanges"]
    if len(values) != len(ledger["tabs"]) + 1:
        raise ValueError("batchGet 응답이 요청한 범위 수와 다릅니다")
    master = parse_taxonomy_master(values[-1].get("values", []), ledger["taxonomy_tab"])
    if not master:
        raise ValueError("매출장 분류마스터가 비어 있습니다")
    product = rules["product"]
    warnings = []
    rows = []
    totals = defaultdict(Decimal)
    for (tab, spec), value_range in zip(
        ledger["tabs"].items(), values[:-1], strict=True
    ):
        columns = {**ledger["columns"], **spec.get("columns", {})}
        for row_number, cells in enumerate(value_range.get("values", [])[1:], start=2):

            def cell(name):
                index = columns.get(name, -1)
                return cells[index] if 0 <= index < len(cells) else ""

            if not any(clean(cell(k)) for k in ("issued_on", "customer", "item")):
                if all(
                    number(cell(k)) in (None, 0) for k in ("amount", "tax", "total")
                ):
                    continue
            try:
                date_text = clean(cell("issued_on"))
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
                    "year": spec["year"],
                    "issued_on": issued_on,
                    "status": status,
                    "customer": clean(cell("customer")),
                    "item": clean(cell("item")),
                    "quantity": number(cell("quantity")),
                    "unit_price": number(cell("unit_price")),
                    "amount": number(cell("amount"), required=True),
                    "tax": number(cell("tax")),
                    "total": number(cell("total")),
                    "category": clean(cell("category")),
                    "subcategory": clean(cell("subcategory")) or None,
                    "note": clean(cell("note")) or None,
                }
            except ValueError as error:
                raise ValueError(f"{tab} {row_number}행: {error}") from error
            original_category = row["category"]
            if original_category not in master:
                row["category"] = product["base"].get(original_category, UNCLASSIFIED)
                for rule in product["exceptions"]:
                    when = rule["when"]
                    if (
                        when.get("year", row["year"]) == row["year"]
                        and when.get("customer_has", "") in row["customer"]
                        and when.get("item_has", "") in row["item"]
                        and any(
                            part in row["item"]
                            for part in when.get("item_has_any", [""])
                        )
                    ):
                        row["category"] = rule["then"]
                        break
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
