import re
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from typing import Any

from gspread.utils import absolute_range_name

from api.google_sheets import DEFAULT_ACCOUNT, get_spreadsheet_values_batch

SPREADSHEET_ID = "1apSjdsEY2mE-KQe3p0jCBypdc5ZKbA4g-6QACPxK7-4"
TABS = {
    "23년 매출장": 2023,
    "24년 매출장": 2024,
    "25년 매출장": 2025,
    "26년 매출장(신)": 2026,
}
UNCLASSIFIED = "미분류"
CATEGORIES = {
    "초등": "1. 코들 라이선스",
    "중등": "1. 코들 라이선스",
    "고등": "1. 코들 라이선스",
    "대학": "1. 코들 라이선스",
    "지자체": "1. 코들 라이선스",
    "기관": "1. 코들 라이선스",
    "교육납품": "1. 코들 라이선스",
    "교육제공": "1. 코들 라이선스",
    "연수제공": "1. 코들 라이선스",
    "연수납품": "1. 코들 라이선스",
    "짓다": "2. 해커톤(짓다)",
    "콘텐츠": "3. 콘텐츠",
    "연구개발": "4. 플랫폼 제공 개발",
    "연수용역": "5. 연수용역",
    "교육용역": "6. 교육용역",
    "기타": "8. 기타",
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def fetch_rows() -> tuple[list[dict], list[str]]:
    ranges = [absolute_range_name(tab, "A:K") for tab in TABS]
    ranges.append(absolute_range_name("분류마스터", "A:L"))
    response = get_spreadsheet_values_batch(
        SPREADSHEET_ID,
        ranges,
        account=DEFAULT_ACCOUNT,
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="FORMATTED_STRING",
    )
    return normalize_rows(response, TABS)


def parse_category_master(rows: list[list]) -> dict[str, list[str]]:
    master = {clean(row[0]): [] for row in rows[2:] if row and clean(row[0])}
    for column in list(zip_longest(*rows[1:], fillvalue=""))[2:11]:
        if major := clean(column[0]):
            master[major] = [clean(value) for value in column[1:] if clean(value)]
    return master


def category_for(customer: str, item: str, year: int, category: str) -> str:
    if (
        ("금성" in customer and ("AIDT" in item or "플랫폼 구축료" in item))
        or ("한국교육학술정보원" in customer and "AIDT" in item)
        or ("잇플" in customer and "AIDT" in item)
        or ("씨마스" in customer and "뷰어" in item)
    ):
        return "4. 플랫폼 제공 개발"
    if (
        ("씨마스" in customer and "저작" in item)
        or "라이브트위커" in item
        or "제이에듀" in customer
        or "모두의 AI 챌린지" in item
    ):
        return "3. 콘텐츠"
    if (
        year == 2025 and "전북특별자치도교육청" in customer and "해커톤" in item
    ) or "짓다" in item:
        return "2. 해커톤(짓다)"
    if "디지털새싹" in item and "반납" in item:
        return "6. 교육용역"
    if year == 2025 and "한양대" in customer and "경기미래형" in item:
        return "5. 연수용역"
    if "아이폰" in item:
        return "8. 기타"
    return CATEGORIES.get(category, UNCLASSIFIED)


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
    response: dict, tabs: dict[str, int] = TABS
) -> tuple[list[dict], list[str]]:
    values = response["valueRanges"]
    if len(values) != len(tabs) + 1:
        raise ValueError("batchGet 응답이 요청한 범위 수와 다릅니다")
    master = parse_category_master(values[-1].get("values", []))
    if not master:
        raise ValueError("매출장 분류마스터가 비어 있습니다")
    warnings = []
    rows = []
    for (tab, year), value_range in zip(tabs.items(), values[:-1], strict=True):
        for row_number, cells in enumerate(value_range.get("values", [])[1:], start=2):
            cells = (cells + [""] * 11)[:11]
            (
                date_value,
                customer,
                item,
                quantity,
                unit_price,
                amount,
                tax,
                total,
                category,
                detail,
                note,
            ) = cells
            if not any(clean(value) for value in (date_value, customer, item)):
                if all(number(value) in (None, 0) for value in (amount, tax, total)):
                    continue
            try:
                date_text = clean(date_value)
                status = (
                    "planned"
                    if date_text in ("예정", "예정분", "계약확정 예정")
                    else "issued"
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
                    "year": year,
                    "issued_on": issued_on,
                    "status": status,
                    "customer": clean(customer),
                    "item": clean(item),
                    "quantity": number(quantity),
                    "unit_price": number(unit_price),
                    "amount": number(amount, required=True),
                    "tax": number(tax),
                    "total": number(total),
                    "category": clean(category),
                    "subcategory": (clean(detail) or None) if year >= 2026 else None,
                    "note": clean(note if year >= 2026 else detail) or None,
                }
            except ValueError as error:
                raise ValueError(f"{tab} {row_number}행: {error}") from error
            original_category = row["category"]
            if original_category not in master:
                row["category"] = category_for(
                    row["customer"], row["item"], year, original_category
                )
            if row["category"] == UNCLASSIFIED:
                warnings.append(f"{tab} {row_number}행: 미분류 «{original_category}»")
            if row["subcategory"] and row["subcategory"] not in master.get(
                original_category, []
            ):
                warnings.append(
                    f"{tab} {row_number}행: 분류마스터에 없는 조합 «{original_category} / {row['subcategory']}»"
                )
            rows.append(row)
    if not rows:
        raise ValueError("매출장 거래가 비어 있습니다. 기존 DB를 유지합니다")
    for target in sorted(set(CATEGORIES.values()) - master.keys()):
        warnings.append(f"분류 규칙의 «{target}»가 분류마스터에 없습니다")
    return rows, warnings
