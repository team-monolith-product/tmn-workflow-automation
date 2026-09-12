"""매출 정규화 테스트용 합성 raw. 시트를 읽지 않는다.

tests/ 에는 __init__.py 가 없고 CI 환경에는 `tests` 라는 이름의 다른 패키지가 설치돼 있어
`from tests.xxx import` 가 엉뚱한 곳을 잡는다. pytest 가 tests/ 를 sys.path 앞에 넣으므로
최상위 모듈로 import 한다.
"""

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
HEADER_2026 = HEADER + ["적요", "비고"]

SOURCES = {
    "ledger": {
        "spreadsheet_id": "sheet-id",
        "spreadsheet_name": "매출장",
        "tabs": {
            "25년 매출장": {"year": 2025},
            "26년 매출장(신)": {
                "year": 2026,
                "columns": {"subcategory_raw": 9, "note": 10},
            },
        },
        "taxonomy_tab": {
            "name": "분류마스터",
            "range": "A1:L40",
            "major_col": 0,
            "major_start_row": 3,
            "detail_header_row": 2,
            "detail_first_col": 2,
            "detail_last_col": 3,
            "detail_start_row": 3,
        },
        "range": "A1:K1200",
        "columns": {
            "date": 0,
            "counterparty": 1,
            "item": 2,
            "qty": 3,
            "unit_price": 4,
            "amount": 5,
            "tax": 6,
            "total": 7,
            "category_raw": 8,
            "subcategory_raw": -1,
            "note": 9,
        },
    },
    "basis": {"mode": "issued", "amount_field": "amount", "pipeline_markers": ["예정"]},
}

TAXONOMY = {
    "version": "test",
    "default_view": "상품",
    "views": {
        "원분류": {
            "label": "매출장 원분류",
            "kind": "passthrough",
            "field": "category_raw",
        },
        "상품": {
            "label": "상품 분류",
            "kind": "rules",
            "child": "세부",
            "prefer_raw": ["1. 코들 라이선스", "2. 해커톤(짓다)"],
            "order": ["1. 코들 라이선스", "2. 해커톤(짓다)"],
            "base": {"고등": "1. 코들 라이선스", "짓다": "2. 해커톤(짓다)"},
            "exceptions": [
                {
                    "id": "aidt-금성",
                    "when": {"counterparty_has": "금성", "item_has": "AIDT"},
                    "then": "2. 해커톤(짓다)",
                }
            ],
        },
        "세부": {
            "label": "세부분류",
            "kind": "passthrough",
            "field": "subcategory_raw",
            "empty": "미기재",
        },
        "학교급": {
            "label": "학교급",
            "kind": "enrichment",
            "field": "school_level",
            "empty": "미상",
        },
    },
    "assertions": {"year_totals_issued": {2025: 2_000_000}, "tolerance_won": 5},
}

MASTER_ROWS = [
    ["대분류", "", "세부 목록"],
    ["", "", "1. 코들 라이선스", "2. 해커톤(짓다)"],
    ["1. 코들 라이선스", "", "학교 판매(초·중·고)", "학교"],
    ["2. 해커톤(짓다)", "", "대학", "대학"],
]


def make_raw() -> dict:
    return {
        "fetched_at": "2026-09-11T07:10:00+09:00",
        "spreadsheet_id": "sheet-id",
        "spreadsheet_title": "매출장",
        "tabs": {
            "25년 매출장": {
                "fiscal_year": 2025,
                "columns": {},
                "rows": [
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
            "26년 매출장(신)": {
                "fiscal_year": 2026,
                "columns": {"subcategory_raw": 9, "note": 10},
                "rows": [
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
        },
        "taxonomy_tab": {"name": "분류마스터", "rows": copy.deepcopy(MASTER_ROWS)},
    }


def build(raw: dict | None = None) -> tuple[list[dict], list[str]]:
    return normalize_rows(
        raw or make_raw(),
        SOURCES,
        {
            "product": TAXONOMY["views"]["상품"],
            "assertions": TAXONOMY["assertions"],
        },
    )
