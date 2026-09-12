import re
from typing import Any

UNCLASSIFIED = "미분류"


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def rule_matches(when: dict[str, Any], tx: dict[str, Any]) -> bool:
    for key, field in (("year", "year"), ("raw", "category_raw"), ("amount", "amount")):
        if key in when and tx[field] != when[key]:
            return False
    for field in ("counterparty", "item"):
        if f"{field}_has" in when and when[f"{field}_has"] not in tx[field]:
            return False
        if f"{field}_regex" in when and not re.search(
            when[f"{field}_regex"], tx[field]
        ):
            return False
    return (
        "item_has_any" not in when
        or any(key in tx["item"] for key in when["item_has_any"])
    ) and ("sub_any" not in when or tx.get("subcategory_raw", "") in when["sub_any"])


def parse_taxonomy_master(
    raw: dict[str, Any], sources: dict[str, Any]
) -> dict[str, list[str]]:
    rows = raw.get("taxonomy_rows") or []
    spec = sources["ledger"].get("taxonomy_tab") or {}
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


def classify(tx: dict, product: dict, master: dict) -> str:
    if tx["category_raw"] in master:
        return tx["category_raw"]
    for rule in product["exceptions"]:
        if rule_matches(rule["when"], tx):
            return rule["then"]
    return product["base"].get(tx["category_raw"], UNCLASSIFIED)
