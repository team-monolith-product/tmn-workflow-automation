import copy

from service.revenue.normalize import normalize_rows

HEADER = [
    "계산서 발행일자",
    "거래처",
    "품목명",
    "수량",
    "단가",
    "공급가액",
    "세액",
    "합계",
    "분류",
]
HEADER_2026 = HEADER + ["세부분류", "비고"]

SOURCES = {
    "ledger": {
        "spreadsheet_id": "sheet-id",
        "tabs": {
            "25년 매출장": {"year": 2025},
            "26년 매출장(신)": {
                "year": 2026,
                "columns": {"subcategory": 9, "note": 10},
            },
        },
        "taxonomy_tab": {
            "name": "분류마스터",
            "range": "A:L",
            "major_col": 0,
            "major_start_row": 3,
            "detail_header_row": 2,
            "detail_first_col": 2,
            "detail_last_col": 3,
            "detail_start_row": 3,
        },
        "range": "A:K",
        "columns": {
            "issued_on": 0,
            "customer": 1,
            "item": 2,
            "quantity": 3,
            "unit_price": 4,
            "amount": 5,
            "tax": 6,
            "total": 7,
            "category": 8,
            "subcategory": -1,
            "note": 9,
        },
    },
    "planned_markers": ["예정"],
}

RULES = {
    "product": {
        "base": {"고등": "1. 코들 라이선스", "짓다": "2. 해커톤(짓다)"},
        "exceptions": [
            {
                "when": {"customer_has": "금성", "item_has": "AIDT"},
                "then": "2. 해커톤(짓다)",
            }
        ],
    },
    "assertions": {"year_totals_issued": {2025: 2000000}, "tolerance_won": 5},
}

MASTER_ROWS = [
    ["대분류", "", "세부 목록"],
    ["", "", "1. 코들 라이선스", "2. 해커톤(짓다)"],
    ["1. 코들 라이선스", "", "학교 판매(초·중·고)", "학교"],
    ["2. 해커톤(짓다)", "", "대학", "대학"],
]


def sheet_response() -> dict:
    return {
        "valueRanges": [
            {
                "values": [
                    HEADER,
                    [
                        "2025-03-02",
                        "호서고등학교",
                        "코들 Basic+",
                        1,
                        1_000_000,
                        1_000_000,
                        100_000,
                        1_100_000,
                        "고등",
                    ],
                    [
                        "2024-12-30",
                        "금성출판사",
                        "AIDT 수익쉐어",
                        1,
                        1_000_000,
                        1_000_000,
                        100_000,
                        1_100_000,
                        "고등",
                    ],
                    ["", "", "", "", "", 0, 0, 0],
                ],
            },
            {
                "values": [
                    HEADER_2026,
                    [
                        "2026-01-05",
                        "도담고등학교",
                        "코들 Pro",
                        200,
                        5_000,
                        1_000_000,
                        100_000,
                        1_100_000,
                        "1. 코들 라이선스",
                        "학교 판매(초·중·고)",
                        "1학기",
                    ],
                    [
                        "2026-02-01",
                        "고려대학교 산학협력단",
                        "해커톤",
                        1,
                        500_000,
                        500_000,
                        50_000,
                        550_000,
                        "2. 해커톤(짓다)",
                        "대학",
                        "",
                    ],
                    [
                        "예정",
                        "금성출판사",
                        "26-1학기 AIDT",
                        "",
                        "",
                        300_000,
                        30_000,
                        330_000,
                        "1. 코들 라이선스",
                        "학교 판매(초·중·고)",
                        "",
                    ],
                ],
            },
            {"values": copy.deepcopy(MASTER_ROWS)},
        ],
    }


def normalized_rows(response: dict | None = None) -> tuple[list[dict], list[str]]:
    return normalize_rows(response or sheet_response(), SOURCES, RULES)
