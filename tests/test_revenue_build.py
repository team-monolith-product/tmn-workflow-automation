from datetime import date
from decimal import Decimal

import pytest
from revenue_fixture import HEADER, HEADER_2026, MASTER_ROWS, SOURCES, build, make_raw

from service.revenue.ledger import fetch_ledger
from service.revenue.normalize import number
from service.revenue.enrich import customer_key, enrich_rows


def test_numeric_preserves_decimals_and_negative_amounts():
    assert number("9,090.123") == Decimal("9090.123")
    assert number("-200.5") == Decimal("-200.5")
    assert number("") is None
    for value in ("invalid", "NaN", "Infinity", ""):
        with pytest.raises(ValueError):
            number(value, required=True)


def test_rows_keep_fiscal_year_planned_and_old_category_mapping():
    rows, warnings = build()
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
    raw = make_raw()
    raw["tabs"]["26년 매출장(신)"]["rows"][1][column] = value
    with pytest.raises(ValueError, match="26년 매출장.*2행"):
        build(raw)


def test_duplicates_and_negative_adjustments_are_preserved():
    raw = make_raw()
    tab = raw["tabs"]["26년 매출장(신)"]["rows"]
    tab.append(list(tab[1]))
    tab.append(
        ["2026-03-01", "반납처", "반납", 1, -100, -100, -10, -110, "1. 코들 라이선스"]
    )
    rows, _ = build(raw)
    assert len(rows) == 7
    assert sum(r["customer"] == "도담고등학교" for r in rows) == 2
    assert rows[-1]["amount"] == -100


def test_category_and_total_problems_are_warnings():
    raw = make_raw()
    raw["tabs"]["25년 매출장"]["rows"][1][5] = 10
    raw["tabs"]["25년 매출장"]["rows"][1][8] = "새분류"
    raw["tabs"]["26년 매출장(신)"]["rows"][1][9] = "없는 세부"
    rows, warnings = build(raw)
    assert rows[0]["category"] == "미분류"
    assert any("총계 불일치" in w for w in warnings)
    assert any("없는 조합" in w for w in warnings)


def test_crm_keeps_all_budgets_and_manual_overrides():
    rows, _ = build()
    warnings = enrich_rows(
        rows,
        [{"school": "도담고등학교", "level": "고등학교", "office": "세종"}],
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
            "manual": {"도담고등학교": {"edu_office": "세종교육청"}},
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


def test_fetch_ledger_는_탭이_없으면_터진다(monkeypatch):
    monkeypatch.setattr(
        "service.revenue.ledger.get_spreadsheet_metadata",
        lambda *_args, **_kwargs: {
            "properties": {"title": "매출장"},
            "sheets": [{"properties": {"title": "26년 매출장(구)"}}],
        },
    )

    with pytest.raises(ValueError, match="매출장에 탭이 없다"):
        fetch_ledger(SOURCES)


def test_fetch_ledger_는_탭과_분류마스터를_한_번에_받는다(monkeypatch):
    calls: dict = {}

    def fake_metadata(_spreadsheet_id, account):
        calls["account"] = account
        return {
            "properties": {"title": "매출장"},
            "sheets": [
                {"properties": {"title": t}}
                for t in ("25년 매출장", "26년 매출장(신)", "분류마스터")
            ],
        }

    def fake_batch(_spreadsheet_id, ranges, **kwargs):
        calls["ranges"] = ranges
        calls["options"] = kwargs
        return {
            "valueRanges": [
                {"values": [HEADER]},
                {"values": [HEADER_2026]},
                {"values": MASTER_ROWS},
            ]
        }

    monkeypatch.setattr(
        "service.revenue.ledger.get_spreadsheet_metadata", fake_metadata
    )
    monkeypatch.setattr(
        "service.revenue.ledger.get_spreadsheet_values_batch", fake_batch
    )

    raw = fetch_ledger(SOURCES)

    assert calls["account"] == "GOOGLE_SERVICE_ACCOUNT_JSON"
    assert calls["ranges"] == [
        "'25년 매출장'!A1:K1200",
        "'26년 매출장(신)'!A1:K1200",
        "'분류마스터'!A1:L40",
    ]
    assert calls["options"]["value_render_option"] == "UNFORMATTED_VALUE"
    assert calls["options"]["date_time_render_option"] == "FORMATTED_STRING"
    assert raw["tabs"]["26년 매출장(신)"]["fiscal_year"] == 2026
    assert raw["tabs"]["26년 매출장(신)"]["columns"] == {
        "subcategory_raw": 9,
        "note": 10,
    }
    assert raw["taxonomy_tab"]["rows"] == MASTER_ROWS
