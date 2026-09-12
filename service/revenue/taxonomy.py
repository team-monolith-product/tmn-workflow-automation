"""배치에서만 원분류를 상품 분류로 환산한다."""

import re
from typing import Any

UNCLASSIFIED = "미분류"


def clean(value: Any) -> str:
    """탭·연속 공백을 정리한다."""
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def rule_matches(when: dict[str, Any], tx: dict[str, Any]) -> bool:
    """when 절의 모든 조건을 만족해야 한다(AND)."""
    if "year" in when and tx["year"] != when["year"]:
        return False
    if "raw" in when and tx["category_raw"] != when["raw"]:
        return False
    if (
        "counterparty_has" in when
        and when["counterparty_has"] not in tx["counterparty"]
    ):
        return False
    if "counterparty_regex" in when and not re.search(
        when["counterparty_regex"], tx["counterparty"]
    ):
        return False
    if "item_has" in when and when["item_has"] not in tx["item"]:
        return False
    if "item_regex" in when and not re.search(when["item_regex"], tx["item"]):
        return False
    if "item_has_any" in when and not any(
        key in tx["item"] for key in when["item_has_any"]
    ):
        return False
    if "sub_any" in when and tx.get("subcategory_raw", "") not in when["sub_any"]:
        return False
    if "amount" in when and tx["amount"] != when["amount"]:
        return False
    return True


def parse_taxonomy_master(
    raw: dict[str, Any], sources: dict[str, Any]
) -> dict[str, list[str]]:
    """매출장 「분류마스터」 탭 -> {대분류: [세부분류...]}.

    분류 정본은 이 표다. 코드가 이름 목록을 따로 들고 있지 않는다.
    시트에서 분류를 늘리면 다음 빌드에 그대로 따라온다.
    """
    blob = raw.get("taxonomy_tab") or {}
    rows = blob.get("rows") or []
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

    # 헤더에만 있고 A열 목록에 없는 대분류도 살린다. 시트가 한쪽만 갱신됐을 수 있다.
    for major in details:
        if major not in majors:
            majors.append(major)
    return {major: details.get(major, []) for major in majors}


def classify(tx: dict, product: dict, master: dict) -> str:
    """원본의 현재 분류 → 첫 예외 규칙 → 옛 분류 대응표 순으로 판정한다."""
    if tx["category_raw"] in (master or product["prefer_raw"]):
        return tx["category_raw"]
    for rule in product["exceptions"]:
        if rule_matches(rule["when"], tx):
            return rule["then"]
    return product["base"].get(tx["category_raw"], UNCLASSIFIED)
