"""매출 facts 빌드 테스트. 시트를 읽지 않고 합성 raw 로 검증한다."""

import copy

import pytest

from service.revenue.build import build_facts, customer_key, normalize, won
from service.revenue.ledger import fetch_ledger
from service.revenue.render import render_health, render_summary, render_transactions
from service.revenue.taxonomy import parse_taxonomy_master

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
    "enrichment": {"file": "knowledge/revenue/enrichment.yml"},
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


def build(raw: dict | None = None, enrichment: dict | None = None) -> dict:
    return build_facts(
        raw or make_raw(), SOURCES, copy.deepcopy(TAXONOMY), enrichment or {}
    )


def test_won_은_문자와_빈칸을_0으로_본다():
    assert won("9,090,909") == 9_090_909
    assert won(909090.9090909091) == 909_091
    assert won("") == 0
    assert won("합계") == 0


def test_customer_key_는_법인_접두어와_공백을_걷어낸다():
    assert customer_key("주식회사 잇플(ITPLE)") == "잇플(ITPLE)"
    assert customer_key("(주)스마트소셜") == "스마트소셜"
    assert customer_key("고려대학교 산학협력단") == "고려대학교산학협력단"


def test_예정_행은_발행분이_아니라_파이프라인이다():
    issued, pipeline, warnings = normalize(make_raw(), SOURCES)

    assert [tx["counterparty"] for tx in issued] == [
        "호서고등학교",
        "금성출판사",
        "도담고등학교",
        "고려대학교 산학협력단",
    ]
    assert [tx["item"] for tx in pipeline] == ["26-1학기 AIDT"]
    assert pipeline[0]["status"] == "pipeline"
    assert pipeline[0]["date"] is None
    assert warnings == []


def test_귀속연도는_탭_기준이고_발행연도가_다르면_비고에_남긴다():
    issued, _, _ = normalize(make_raw(), SOURCES)
    deferred = next(tx for tx in issued if tx["counterparty"] == "금성출판사")

    assert deferred["year"] == 2025
    assert deferred["issue_year"] == 2024
    assert deferred["month"] == "2024-12"
    assert "발행연도와 귀속연도 불일치" in deferred["note"]


def test_탭별_열_배치를_따른다():
    issued, _, _ = normalize(make_raw(), SOURCES)
    new = next(tx for tx in issued if tx["counterparty"] == "도담고등학교")
    old = next(tx for tx in issued if tx["counterparty"] == "호서고등학교")

    assert new["subcategory_raw"] == "학교 판매(초·중·고)"
    assert new["note"] == "1학기"
    assert old["subcategory_raw"] == ""


def test_날짜를_못_읽은_행은_빼고_경고한다():
    raw = make_raw()
    raw["tabs"]["25년 매출장"]["rows"].append(
        ["3월중", "누구", "무엇", 1, 1, 1, 0, 1, "고등"]
    )
    issued, _, warnings = normalize(raw, SOURCES)

    assert all(tx["counterparty"] != "누구" for tx in issued)
    assert len(warnings) == 1
    assert "3월중" in warnings[0]


def test_2026은_매출장_대분류를_그대로_쓰고_2025는_규칙으로_환산한다():
    facts = build()
    by_name = {tx["counterparty"]: tx for tx in facts["transactions"]}

    assert by_name["도담고등학교"]["views"]["상품"] == "1. 코들 라이선스"
    assert by_name["도담고등학교"]["rule"]["상품"] == "매출장 분류 그대로"
    assert by_name["호서고등학교"]["views"]["상품"] == "1. 코들 라이선스"
    assert "상품" not in by_name["호서고등학교"]["rule"]
    # 예외 규칙이 base 보다 먼저 걸린다
    assert by_name["금성출판사"]["views"]["상품"] == "2. 해커톤(짓다)"
    assert by_name["금성출판사"]["rule"]["상품"] == "aidt-금성"


def test_집계와_검증이_맞으면_경고가_없다():
    facts = build()

    assert facts["meta"]["problems"] == []
    assert facts["aggregates"]["by_year"] == {"2025": 2_000_000, "2026": 1_500_000}
    assert facts["aggregates"]["by_month"]["2024-12"] == 1_000_000
    assert facts["aggregates"]["by_view"]["상품"]["2026"] == {
        "amount": {"1. 코들 라이선스": 1_000_000, "2. 해커톤(짓다)": 500_000},
        "count": {"1. 코들 라이선스": 1, "2. 해커톤(짓다)": 1},
    }
    assert facts["pipeline"] == {
        "total": 300_000,
        "count": 1,
        "transactions": facts["pipeline"]["transactions"],
    }
    # 파이프라인은 연도 집계에 섞이지 않는다
    assert sum(facts["aggregates"]["by_year"].values()) == 3_500_000


def test_분류마스터가_뷰_순서와_prefer_raw_를_덮어쓴다():
    raw = make_raw()
    raw["taxonomy_tab"]["rows"].append(["9. AI 캠프(코들)", "", "", ""])
    facts = build(raw)

    master = facts["meta"]["taxonomy_master"]
    assert list(master) == ["1. 코들 라이선스", "2. 해커톤(짓다)", "9. AI 캠프(코들)"]
    assert master["1. 코들 라이선스"] == ["학교 판매(초·중·고)", "대학"]
    assert master["2. 해커톤(짓다)"] == ["학교", "대학"]
    assert facts["meta"]["views"]["상품"]["order"] == list(master)
    assert facts["meta"]["views"]["세부"]["order"] == [
        "학교 판매(초·중·고)",
        "대학",
        "학교",
    ]


def test_분류마스터에_없는_세부분류_조합은_경고한다():
    raw = make_raw()
    raw["tabs"]["26년 매출장(신)"]["rows"][1][9] = "없는 세부"
    facts = build(raw)

    assert any(
        "«없는 세부»는 분류마스터에 없는 세부분류" in p
        for p in facts["meta"]["problems"]
    )


def test_규칙이_가리키는_대분류가_마스터에_없으면_경고한다():
    raw = make_raw()
    raw["taxonomy_tab"]["rows"] = [
        ["대분류", "", "세부"],
        ["", "", "1. 코들 라이선스"],
        ["1. 코들 라이선스", "", "학교"],
    ]
    facts = build(raw)

    assert any(
        "«2. 해커톤(짓다)»로 보내는데 분류마스터에 그런 대분류가 없다" in p
        for p in facts["meta"]["problems"]
    )


def test_연도_총계가_기준값과_어긋나면_경고한다():
    raw = make_raw()
    raw["tabs"]["25년 매출장"]["rows"][1][5] = 1_000_010
    facts = build(raw)

    assert any(
        "연도 총계 불일치 2025" in p and "+10" in p for p in facts["meta"]["problems"]
    )


def test_규칙에_안_걸린_원분류는_미분류로_남기고_경고한다():
    raw = make_raw()
    raw["tabs"]["25년 매출장"]["rows"][1][8] = "신규분류"
    facts = build(raw)

    tx = next(t for t in facts["transactions"] if t["counterparty"] == "호서고등학교")
    assert tx["views"]["상품"] == "미분류"
    assert any("«신규분류»" in p and "미분류" in p for p in facts["meta"]["problems"])


def test_보강값은_조회표에_한_번만_두고_manual_이_이긴다():
    enrichment = {
        "customers": {
            "도담고등학교": {"school_level": "고등학교", "edu_office": "세종"}
        },
        "manual": {"도담고등학교": {"edu_office": "세종특별자치시교육청"}},
        "program_aliases": {"해커톤": "2026 대학 해커톤"},
        "programs": {
            "2026 대학 해커톤": {"name": "2026 대학 해커톤", "client": "고려대"}
        },
    }
    facts = build(enrichment=enrichment)
    by_name = {tx["counterparty"]: tx for tx in facts["transactions"]}

    assert by_name["도담고등학교"]["views"]["학교급"] == "고등학교"
    assert "enrich" not in by_name["도담고등학교"]
    assert facts["customers"]["도담고등학교"]["edu_office"] == "세종특별자치시교육청"
    assert by_name["고려대학교 산학협력단"]["program_key"] == "2026 대학 해커톤"
    assert list(facts["programs"]) == ["2026 대학 해커톤"]
    assert by_name["호서고등학교"]["views"]["학교급"] == "미상"


def test_사업_별칭이_마스터에_없는_이름을_가리키면_경고한다():
    facts = build(
        enrichment={"program_aliases": {"해커톤": "유령 사업"}, "programs": {}}
    )

    assert any("«유령 사업»" in p for p in facts["meta"]["problems"])


def test_parse_taxonomy_master_는_헤더에만_있는_대분류도_살린다():
    raw = make_raw()
    raw["taxonomy_tab"]["rows"] = [
        ["대분류", "", "세부"],
        ["", "", "1. 코들 라이선스", "7. 글로벌"],
        ["1. 코들 라이선스", "", "학교", "UAE"],
    ]

    assert parse_taxonomy_master(raw, SOURCES) == {
        "1. 코들 라이선스": ["학교"],
        "7. 글로벌": ["UAE"],
    }


def test_요약은_최근_연도를_기본으로_하고_예정분을_따로_적는다():
    text = render_summary(build())

    assert "2026년 상품 분류" in text
    assert "| 1. 코들 라이선스 | 1,000,000 | 1건 | 66.7% |" in text
    assert "| 1. 코들 라이선스 | 학교 판매(초·중·고) | 1,000,000 |" in text
    assert "파이프라인 (매출 아님)" in text and "300,000원" in text
    assert "검증 경고 없음" in text


def test_요약은_없는_연도를_말해_준다():
    assert render_summary(build(), 2019).startswith("2019년 발행 매출이 없다")


def test_거래_목록은_조건으로_거르고_금액_큰_순이다():
    facts = build()

    text = render_transactions(facts, year=2025)
    assert "2건 2,000,000원" in text
    assert (
        text.index("2025-03-02") < text.index("2024-12-30")
        or text.count("1,000,000") >= 2
    )

    # 2025 금성 건은 예외 규칙으로 해커톤 버킷에 들어가 있다. 뷰 버킷도 검색 대상이다.
    text = render_transactions(facts, category="해커톤")
    assert "2건 1,500,000원" in text

    text = render_transactions(facts, customer="금성", status="all")
    assert "2건 1,300,000원" in text
    assert "| pipeline | 예정 |" in text

    text = render_transactions(facts, year=2026, limit=1)
    assert "상위 1건만 보인다" in text


def test_검증_경고가_있으면_health_가_전부_나열한다():
    raw = make_raw()
    raw["tabs"]["25년 매출장"]["rows"][1][8] = "신규분류"
    text = render_health(build(raw))

    assert "검증 경고" in text and "확정으로 쓰지 않는다" in text
    assert "«신규분류»" in text
    assert "검증 경고 없음" in render_health(build())


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
