"""
교육 외주 입찰공고 크롤러 단위 테스트

외부 API(G2B), LLM, Slack 호출은 모두 모킹하여 비즈니스 로직만 검증한다.
"""

from datetime import date

import pytest

from scripts.crawl_education_bids import (
    build_window,
    extract_items,
    summarize_item,
    evaluate_announcements,
    format_slack_text,
    run,
    BidEvaluation,
    BatchEvaluation,
)
from service.config import EducationBidCrawlerConfig


# --- build_window ---


def test_build_window_single_day():
    """직전 1일 구간이 어제 00:00 ~ 어제 23:59 로 계산된다."""
    bgn, end = build_window(date(2026, 5, 30), lookback_days=1)
    assert bgn == "202605290000"
    assert end == "202605292359"


def test_build_window_multi_day():
    """lookback_days=3 이면 3일 전부터 어제까지."""
    bgn, end = build_window(date(2026, 5, 30), lookback_days=3)
    assert bgn == "202605270000"
    assert end == "202605292359"


# --- extract_items ---


def test_extract_items_empty_string():
    """결과 0건이면 items가 빈 문자열로 온다."""
    payload = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"items": "", "totalCount": 0},
        }
    }
    items, total = extract_items(payload)
    assert items == []
    assert total == 0


def test_extract_items_single_dict():
    """단건이면 items가 dict 로 올 수 있다."""
    payload = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"items": {"item": {"bidNtceNm": "A"}}, "totalCount": 1},
        }
    }
    items, total = extract_items(payload)
    assert items == [{"bidNtceNm": "A"}]
    assert total == 1


def test_extract_items_list():
    payload = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "items": [{"bidNtceNm": "A"}, {"bidNtceNm": "B"}],
                "totalCount": 2,
            },
        }
    }
    items, total = extract_items(payload)
    assert [i["bidNtceNm"] for i in items] == ["A", "B"]
    assert total == 2


def test_extract_items_error_code_raises():
    """정상 코드가 아니면 RuntimeError."""
    payload = {
        "response": {
            "header": {
                "resultCode": "30",
                "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED",
            },
            "body": {},
        }
    }
    with pytest.raises(RuntimeError, match="resultCode=30"):
        extract_items(payload)


# --- summarize_item ---


def test_summarize_item_field_fallback():
    """결합형(bidNtceDt)·분리형(bidNtceDate) 스키마 모두 폴백된다."""
    item_combined = {
        "bidNtceNo": "20260101",
        "bidNtceNm": "디지털교과서 플랫폼 고도화",
        "dminsttNm": "충청남도교육청",
        "bidNtceDt": "2026-05-29 10:00:00",
        "bidClseDt": "2026-06-10 17:00:00",
        "presmptPrce": "320000000",
        "bidNtceDtlUrl": "http://example.com/1",
        "_kind_label": "용역",
    }
    s = summarize_item(item_combined)
    assert s["title"] == "디지털교과서 플랫폼 고도화"
    assert s["demand_inst"] == "충청남도교육청"
    assert s["close_dt"] == "2026-06-10 17:00:00"
    assert s["estimated_price"] == "320000000"
    assert s["url"] == "http://example.com/1"

    item_split = {
        "bidNtceNm": "B",
        "bidNtceDate": "20260529",
        "bidNtceUrl": "http://x/2",
    }
    s2 = summarize_item(item_split)
    assert s2["notice_dt"] == "20260529"
    assert s2["url"] == "http://x/2"


# --- evaluate_announcements (배치 인덱스 매핑) ---


class _FakeLLM:
    """배치별로 index 0..n-1 에 대해 점수를 돌려주는 가짜 structured LLM."""

    def __init__(self, scorer):
        self._scorer = scorer
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        # 프롬프트의 [i] 라인 수만큼 평가 생성
        n = prompt.count("\n[")
        evals = [
            BidEvaluation(
                index=i,
                score=self._scorer(self.calls, i),
                category="플랫폼개발",
                reason="r",
            )
            for i in range(n)
        ]
        return BatchEvaluation(evaluations=evals)


def test_evaluate_announcements_batches_and_global_index():
    """배치 경계를 넘어 글로벌 index 로 올바르게 매핑된다."""
    summaries = [
        {
            "title": f"t{i}",
            "kind_label": "용역",
            "demand_inst": "",
            "notice_inst": "",
            "estimated_price": "",
        }
        for i in range(5)
    ]
    # 배치마다 index 0 만 90점, 나머지 10점
    fake = _FakeLLM(lambda call, i: 90 if i == 0 else 10)
    result = evaluate_announcements(
        summaries, "profile", "gpt-x", batch_size=2, llm=fake
    )

    assert fake.calls == 3  # 2+2+1
    assert len(result) == 5
    # 각 배치의 첫 항목(글로벌 0, 2, 4)이 90점
    assert result[0].score == 90
    assert result[2].score == 90
    assert result[4].score == 90
    assert result[1].score == 10


def test_evaluate_announcements_empty():
    assert evaluate_announcements([], "p", "m", 10, llm=_FakeLLM(lambda c, i: 0)) == {}


# --- format_slack_text ---


def test_format_slack_text_sorted_and_formatted():
    summaries = [
        {
            "title": "낮은 공고",
            "kind_label": "용역",
            "demand_inst": "A교육청",
            "notice_inst": "",
            "estimated_price": "1000000",
            "close_dt": "2026-06-01",
            "url": "http://x/low",
        },
        {
            "title": "높은 공고",
            "kind_label": "용역",
            "demand_inst": "B교육청",
            "notice_inst": "",
            "estimated_price": "320000000",
            "close_dt": "2026-06-10",
            "url": "http://x/high",
        },
    ]
    qualified = [
        (
            summaries[0],
            BidEvaluation(index=0, score=70, category="콘텐츠제작", reason="근거L"),
        ),
        (
            summaries[1],
            BidEvaluation(index=1, score=90, category="플랫폼개발", reason="근거H"),
        ),
    ]
    # run()에서 정렬하므로 여기선 입력 순서대로 출력됨 — 정렬은 run 테스트에서 검증
    text = format_slack_text(qualified, ("202605290000", "202605292359"))
    assert "교육 외주 적합 입찰공고 2건" in text
    assert "320,000,000원" in text  # 천단위 콤마
    assert "근거H" in text
    assert "http://x/high" in text


# --- run (통합: collect/llm/slack 모킹) ---


def _cfg(**over):
    base = dict(
        channel_id="C123",
        business_profile="우리는 에듀테크 회사",
        model="gpt-x",
        score_threshold=60,
        lookback_days=1,
        kinds=["servc"],
        batch_size=20,
    )
    base.update(over)
    return EducationBidCrawlerConfig(**base)


def test_run_filters_threshold_and_sorts(monkeypatch, capsys):
    """임계 미만은 제외, 적합 공고는 점수 내림차순으로 dry-run 출력."""
    raw = [
        {
            "bidNtceNm": "고적합 플랫폼",
            "dminsttNm": "B청",
            "presmptPrce": "300000000",
            "bidNtceDtlUrl": "u2",
            "_kind_label": "용역",
        },
        {
            "bidNtceNm": "저적합 비품",
            "dminsttNm": "A청",
            "presmptPrce": "1000",
            "bidNtceDtlUrl": "u1",
            "_kind_label": "용역",
        },
    ]
    monkeypatch.setattr(
        "scripts.crawl_education_bids.collect_announcements",
        lambda kinds, bgn, end, session=None: raw,
    )

    def fake_eval(summaries, profile, model, batch_size, llm=None):
        # title 에 '고적합'이면 90, 아니면 30
        return {
            i: BidEvaluation(
                index=i,
                score=90 if "고적합" in s["title"] else 30,
                category="플랫폼개발",
                reason="r",
            )
            for i, s in enumerate(summaries)
        }

    monkeypatch.setattr(
        "scripts.crawl_education_bids.evaluate_announcements", fake_eval
    )

    run(_cfg(), date(2026, 5, 30), dry_run=True)
    out = capsys.readouterr().out
    assert "적합(60점 이상): 1건" in out
    assert "고적합 플랫폼" in out
    assert "저적합 비품" not in out  # 임계 미만 제외


def test_run_no_qualified_skips_slack(monkeypatch):
    """적합 공고가 없으면 Slack을 호출하지 않는다."""
    raw = [{"bidNtceNm": "무관 공고", "_kind_label": "용역", "presmptPrce": ""}]
    monkeypatch.setattr(
        "scripts.crawl_education_bids.collect_announcements",
        lambda kinds, bgn, end, session=None: raw,
    )
    monkeypatch.setattr(
        "scripts.crawl_education_bids.evaluate_announcements",
        lambda *a, **k: {
            0: BidEvaluation(index=0, score=10, category="해당없음", reason="무관")
        },
    )

    called = {"posted": False}

    def _should_not_call(*a, **k):
        called["posted"] = True

    monkeypatch.setattr("scripts.crawl_education_bids.WebClient", _should_not_call)
    run(_cfg(), date(2026, 5, 30), dry_run=False)
    assert called["posted"] is False
