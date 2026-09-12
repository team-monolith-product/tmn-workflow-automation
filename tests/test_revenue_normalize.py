from datetime import date
from decimal import Decimal

import pytest
from revenue_fixture import (
    RULES,
    SOURCES,
    normalized_rows,
    sheet_response,
)

from service.revenue.normalize import fetch_rows, number
from service.revenue.enrich import customer_key, enrich_rows


def test_numeric_preserves_decimals_and_negative_amounts():
    assert number("9,090.123") == Decimal("9090.123")
    assert number("-200.5") == Decimal("-200.5")
    assert number("") is None
    for value in ("invalid", "NaN", "Infinity", ""):
        with pytest.raises(ValueError):
            number(value, required=True)


def test_rows_keep_fiscal_year_planned_and_old_category_mapping():
    rows, warnings = normalized_rows()
    assert warnings == []
    assert len(rows) == 5
    old = next(
        r for r in rows if r["customer"] == "금성출판사" and r["status"] == "issued"
    )
    assert old["year"] == 2025 and old["issued_on"] == date(2024, 12, 30)
    assert old["category"] == "2. 해커톤(짓다)"
    new = next(r for r in rows if r["customer"] == "도담고등학교")
    assert new["category"] == "1. 코들 라이선스"
    assert new["subcategory"] == "학교 판매(초·중·고)" and new["note"] == "1학기"
    planned = next(r for r in rows if r["status"] == "planned")
    assert planned["issued_on"] is None
    assert sum(r["amount"] for r in rows if r["status"] == "issued") == 3500000


@pytest.mark.parametrize(
    "column,value", [(0, "2026-02-30"), (0, "3월중"), (0, ""), (5, "합계")]
)
def test_invalid_transaction_fails_instead_of_silently_dropping(column, value):
    raw = sheet_response()
    raw["valueRanges"][1]["values"][1][column] = value
    with pytest.raises(ValueError, match="26년 매출장.*2행"):
        normalized_rows(raw)


def test_duplicates_and_negative_adjustments_are_preserved():
    raw = sheet_response()
    tab = raw["valueRanges"][1]["values"]
    tab.append(list(tab[1]))
    tab.append(
        ["2026-03-01", "반납처", "반납", 1, -100, -100, -10, -110, "1. 코들 라이선스"]
    )
    rows, _ = normalized_rows(raw)
    assert len(rows) == 7
    assert sum(r["customer"] == "도담고등학교" for r in rows) == 2
    assert rows[-1]["amount"] == -100


def test_category_problems_are_warnings():
    raw = sheet_response()
    raw["valueRanges"][0]["values"][1][8] = "새분류"
    raw["valueRanges"][1]["values"][1][9] = "없는 세부"
    rows, warnings = normalized_rows(raw)
    assert rows[0]["category"] == "미분류"
    assert any("없는 조합" in w for w in warnings)


def test_crm_keeps_all_budgets_and_program_fields():
    rows, _ = normalized_rows()
    warnings = enrich_rows(
        rows,
        [{"school": "도담고등학교", "level": "고등학교", "office": "세종교육청"}],
        [
            {
                "school": "도담고등학교",
                "budget": ["자체예산", "정책예산"],
                "terms": ["1학기", "2학기"],
            }
        ],
        [
            {
                "name": "대학 해커톤",
                "client": "고려대",
                "budget": "1,000.5",
                "our_revenue": 500,
                "stage": "진행",
            }
        ],
        {
            "program_aliases": {"해커톤": "대학 해커톤"},
        },
    )
    assert warnings == []
    school = next(r for r in rows if r["customer"] == "도담고등학교")
    assert school["budget_sources"] == ["자체예산", "정책예산"]
    assert school["terms"] == ["1학기", "2학기"]
    assert school["edu_office"] == "세종교육청"
    program = next(r for r in rows if "해커톤" in r["item"])
    assert program["program_budget"] == Decimal("1000.5")
    assert program["program_name"] == "대학 해커톤"


def test_ambiguous_school_does_not_pick_an_arbitrary_office():
    rows = [{"customer": "도담고", "item": "코들"}]
    enrich_rows(
        rows,
        [
            {"school": "도담고등학교", "office": "A"},
            {"school": "도담고등학교", "office": "B"},
        ],
        [],
        [],
        {},
    )
    assert rows[0]["school_level"] == "고등학교"
    assert rows[0].get("edu_office") is None
    assert customer_key("(주) 가나다") == "가나다"


def test_fetch_failure_propagates(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("invalid range")

    monkeypatch.setattr("service.revenue.normalize.get_spreadsheet_values_batch", fail)
    with pytest.raises(RuntimeError, match="invalid range"):
        fetch_rows(SOURCES, RULES)


def test_fetch_normalizes_all_ranges_in_one_request(monkeypatch):
    calls = []

    def fake_batch(spreadsheet_id, ranges, **kwargs):
        calls.append((spreadsheet_id, ranges, kwargs))
        return sheet_response()

    monkeypatch.setattr(
        "service.revenue.normalize.get_spreadsheet_values_batch", fake_batch
    )
    assert fetch_rows(SOURCES, RULES) == normalized_rows()
    assert len(calls) == 1
    spreadsheet_id, ranges, options = calls[0]
    assert spreadsheet_id == SOURCES["ledger"]["spreadsheet_id"]
    assert ranges == ["'25년 매출장'!A:K", "'26년 매출장(신)'!A:K", "'분류마스터'!A:L"]
    assert options == {
        "account": "GOOGLE_SERVICE_ACCOUNT_JSON",
        "value_render_option": "UNFORMATTED_VALUE",
        "date_time_render_option": "FORMATTED_STRING",
    }


def test_incomplete_ranges_fail():
    response = sheet_response()
    response["valueRanges"].pop()
    with pytest.raises(ValueError, match="범위 수"):
        normalized_rows(response)


def test_empty_category_master_fails():
    response = sheet_response()
    response["valueRanges"][-1] = {}
    with pytest.raises(ValueError, match="분류마스터가 비어"):
        normalized_rows(response)
