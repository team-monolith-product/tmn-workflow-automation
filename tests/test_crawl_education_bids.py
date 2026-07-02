"""
교육 외주 입찰 파이프라인 단계 테스트

네트워크·LLM 은 모킹/모듈 경계에서 차단하고 순수 변환 로직을 검증한다.
"""

from service.edu_bid import stages, evaluate
from service.edu_bid.knowledge import load_knowledge, load_shared_knowledge
from service.edu_bid.schemas import Announcement, GateResult, Axes, EvalOut, BatchEval


def _ann(**over) -> Announcement:
    base = dict(
        kind_label="용역",
        bid_no="R1",
        bid_ord="000",
        title="t",
        notice_inst="",
        demand_inst="",
        close_dt="",
        estimated_price="",
        url="",
        award_method="",
        re_notice="N",
        result_competition="N",
        industry_limit="N",
        region_limit_basis="",
        tech_eval_rate="",
        price_eval_rate="",
        info_biz="N",
        service_div="",
        proc_class="",
    )
    base.update(over)
    return Announcement(**base)


# --- to_announcement (실제 필드 매핑) ---


def test_to_announcement_maps_signal_fields():
    item = {
        "bidNtceNo": "R26BK01550144",
        "bidNtceOrd": "000",
        "bidNtceNm": "디지털교과서 플랫폼 고도화",
        "dminsttNm": "충남교육청",
        "bidClseDt": "2026-06-10 17:00:00",
        "presmptPrce": "320000000",
        "bidNtceDtlUrl": "http://x/1",
        "sucsfbidMthdNm": "협상에 의한 계약",
        "arsltCmptYn": "N",
        "indstrytyLmtYn": "Y",
        "infoBizYn": "Y",
        "ntceSpecFileNm1": "제안요청서.hwp",
        "ntceSpecDocUrl1": "http://spec/1",
        "ntceSpecDocUrl2": "http://spec/2",
    }
    a = stages.to_announcement(item, "용역")
    assert a.title == "디지털교과서 플랫폼 고도화"
    assert a.award_method == "협상에 의한 계약"
    assert a.result_competition == "N"
    assert a.industry_limit == "Y"
    assert a.info_biz == "Y"
    assert a.spec_docs == [
        {"name": "제안요청서.hwp", "url": "http://spec/1"},
        {"name": "", "url": "http://spec/2"},
    ]
    assert a.bid_no == "R26BK01550144"


# --- to_announcement_prespec (사전규격) ---


def test_to_announcement_prespec_maps_fields():
    item = {
        "bfSpecRgstNo": "R26BD00232047",
        "prdctClsfcNoNm": "AI 코딩교육 플랫폼 구축",
        "orderInsttNm": "조달청 광주지방조달청",
        "rlDminsttNm": "○○대학교",
        "asignBdgtAmt": "161526000",
        "rcptDt": "2026-05-26 07:40:57",
        "opninRgstClseDt": "2026-05-31 23:59:00",
        "swBizObjYn": "Y",
        "bsnsDivNm": "일반용역",
        "prdctDtlList": "[1^8111159901^정보시스템개발서비스]",
        "specDocFileUrl1": "http://spec/1",
    }
    a = stages.to_announcement_prespec(item, "용역(사전규격)")
    assert a.stage == "presearch"
    assert a.title == "AI 코딩교육 플랫폼 구축"
    assert a.demand_inst == "○○대학교"
    assert a.estimated_price == "161526000"
    assert a.opinion_close_dt == "2026-05-31 23:59:00"
    assert a.bid_no == "R26BD00232047"
    assert a.proc_class == "정보시스템개발서비스"  # prdctDtlList 세부품명
    assert a.spec_docs == [{"name": "규격서1", "url": "http://spec/1"}]
    # 사전규격엔 낙찰방식 없음 → 게이트 수의시담/실적 룰 비적용
    assert a.award_method == "" and a.result_competition == ""


# --- dedupe ---


def test_dedupe_keeps_latest_order():
    items = [
        _ann(bid_no="R1", bid_ord="000", title="A전"),
        _ann(bid_no="R1", bid_ord="001", title="A후"),
        _ann(bid_no="R2", bid_ord="000", title="B"),
    ]
    out = stages.dedupe_by_notice(items)
    by_no = {a.bid_no: a for a in out}
    assert len(out) == 2
    assert by_no["R1"].bid_ord == "001"


# --- gate ---


def test_gate_result_competition_near_miss_when_weak_performance():
    """실적경쟁=Y + 실적원장 비어있음 → near_miss."""
    g = stages.gate(_ann(result_competition="Y"), {"performance": []})
    assert g.status == "near_miss"
    assert any("실적제한경쟁" in r for r in g.reasons)


def test_gate_fail_on_sucsfbid_sidam():
    """수의시담/다자간수의시담 = 정보공개용 → 참가 불가(fail). 원장의 제외 낙찰방식 룰 적용."""
    g = stages.gate(
        _ann(award_method="수의시담-일반경쟁->수의"),
        {"performance": [], "excluded_award_methods": ["수의시담"]},
    )
    assert g.status == "fail"
    assert any("참가 불가" in r for r in g.reasons)


def test_gate_pass_on_small_sum_quote():
    """소액수의견적(2인 이상 견적 제출)은 견적 경쟁 → 제외 룰에 안 걸림(참가 가능)."""
    g = stages.gate(
        _ann(award_method="소액수의견적-소액수의견적(2인 이상 견적 제출)"),
        {"performance": [], "excluded_award_methods": ["수의시담"]},
    )
    assert g.status == "pass"


def test_gate_pass_with_region_industry_flags():
    g = stages.gate(
        _ann(region_limit_basis="공고서 참조", industry_limit="Y"), {"performance": []}
    )
    assert g.status == "pass"
    assert any("지역제한" in r for r in g.reasons)
    assert any("업종제한" in r for r in g.reasons)


# --- triage ---


def test_triage_matches_capability_keyword():
    kw_index = {"codle": ["코딩교육", "LMS"], "judge": ["자동채점"]}
    matched = stages.triage(_ann(title="초중등 코딩교육 플랫폼 구축"), kw_index)
    assert "codle" in matched and "judge" not in matched


def test_triage_no_match():
    kw_index = {"codle": ["코딩교육"]}
    assert stages.triage(_ann(title="가로수 전지 용역"), kw_index) == []


# --- decide (가중합·라벨) ---


class _KN:
    weights = {
        "reuse": 0.4,
        "winnability": 0.3,
        "value": 0.2,
        "performance_building": 0.1,
    }
    thresholds = {"recommend": 70, "review": 50}


def _eval(reuse, winnability, value, performance_building, **over) -> EvalOut:
    base = dict(
        index=0,
        axes=Axes(
            reuse=reuse,
            winnability=winnability,
            value=value,
            performance_building=performance_building,
        ),
        quant_barrier="low",
        wired_risk="low",
        matched_assets=["codle"],
        rationale="근거",
    )
    base.update(over)
    return EvalOut(**base)


def test_decide_recommend_label():
    ev = _eval(90, 80, 60, 40)
    d = stages.decide(_ann(), GateResult("pass"), ev, [], _KN())
    # 0.4*90+0.3*80+0.2*60+0.1*40 = 36+24+12+4 = 76
    assert d.score == 76.0 and d.label == "입찰추천" and d.wired_risk == "low"
    assert d.matched_assets == ["codle"]


def test_decide_falls_back_to_triage_assets_when_eval_empty():
    ev = _eval(90, 80, 60, 40, matched_assets=[])
    d = stages.decide(_ann(), GateResult("pass"), ev, ["codle", "judge"], _KN())
    assert d.matched_assets == ["codle", "judge"]


def test_decide_near_miss_gate_overrides_to_future_target():
    ev = _eval(
        90, 90, 90, 90, quant_barrier="high", wired_risk="high", matched_assets=[]
    )
    d = stages.decide(_ann(), GateResult("near_miss", ["실적"]), ev, [], _KN())
    assert d.label == "미래타깃"


# --- format_report ---


def test_format_report_sorts_and_filters():
    from service.edu_bid.schemas import Decision

    a_hi = _ann(title="추천건", estimated_price="300000000", demand_inst="A청")
    a_rv = _ann(title="검토건", estimated_price="1000000", demand_inst="B청")
    a_ex = _ann(title="제외건")
    decisions = [
        Decision(
            a_rv,
            GateResult("pass"),
            {"reuse": 60, "winnability": 50, "value": 50, "performance_building": 40},
            "low",
            ["x"],
            55.0,
            "검토",
            "r1",
        ),
        Decision(
            a_hi,
            GateResult("pass"),
            {"reuse": 90, "winnability": 85, "value": 70, "performance_building": 60},
            "none",
            ["codle"],
            82.0,
            "입찰추천",
            "r2",
        ),
        Decision(
            a_ex,
            GateResult("pass"),
            {"reuse": 10, "winnability": 10, "value": 10, "performance_building": 10},
            "high",
            [],
            10.0,
            "제외",
            "r3",
        ),
    ]
    text = stages.format_report(
        decisions, ("202605290000", "202605292359"), "구축/개발"
    )
    assert "후보 2건" in text  # 제외 제외됨
    assert "[구축/개발]" in text  # 트랙명 헤더
    assert text.index("추천건") < text.index("검토건")  # 추천이 먼저
    assert "300,000,000원" in text
    assert "제외건" not in text


# --- evaluate (배치 index 매핑, LLM 모킹) ---


class _FakeLLM:
    def invoke(self, prompt):
        n = prompt.count("\n[")
        return BatchEval(
            evaluations=[
                EvalOut(
                    index=i,
                    axes=Axes(
                        reuse=80, winnability=70, value=60, performance_building=50
                    ),
                    quant_barrier="low",
                    wired_risk="low",
                    matched_assets=["codle"],
                    rationale="r",
                )
                for i in range(n)
            ]
        )


def test_evaluate_batches_and_global_index():
    kn = load_knowledge("dev")  # 프롬프트 조립에 실제 지식 사용(네트워크 없음)
    cands = [(_ann(title=f"코딩교육 {i}"), ["codle"]) for i in range(5)]
    out = evaluate.evaluate(cands, kn, "gpt-x", batch_size=2, llm=_FakeLLM())
    assert len(out) == 5
    assert all(e.axes.reuse == 80 for e in out.values())


def test_evaluate_empty():
    assert evaluate.evaluate([], load_knowledge("dev"), "m", 10, llm=_FakeLLM()) == {}


# --- enrich (S4) ---


def test_clean_text_strips_record_tag_noise():
    from service.edu_bid import enrich

    # HWP 레코드 태그 깨짐(捤獥 등 CJK 한자)은 제거되고 한글/숫자는 보존
    out = enrich.clean_text("捤獥 사업범위 1. 과업 内容 abc 123")
    assert "사업범위" in out and "과업" in out and "abc" in out and "123" in out
    assert "捤獥" not in out and "内" not in out


def test_enrich_ranks_and_budgets(monkeypatch):
    from service.edu_bid import enrich

    a = _ann(
        spec_docs=[
            {"name": "1. 공고문.pdf", "url": "u_pdf"},
            {"name": "2. 제안요청서.hwp", "url": "u_rfp"},
        ]
    )
    # 제안요청서가 먼저 정독되도록 우선순위 확인 + 다운로드/추출 모킹
    monkeypatch.setattr(enrich, "_download", lambda url, session: url.encode())
    monkeypatch.setattr(enrich, "extract_text", lambda content, name: f"본문<{name}>")
    text = enrich.enrich(a, char_budget=1000)
    assert text.index("제안요청서") < text.index("공고문")  # RFP 먼저


def test_enrich_skips_failed_download(monkeypatch):
    from service.edu_bid import enrich

    a = _ann(spec_docs=[{"name": "x.hwp", "url": "u"}])

    def boom(url, session):
        raise RuntimeError("net")

    monkeypatch.setattr(enrich, "_download", boom)
    assert enrich.enrich(a) == ""  # 실패해도 예외 없이 빈 문자열


# --- classify_work_type (S3.5) ---


def test_classify_work_type():
    wt = load_shared_knowledge().work_types
    assert (
        stages.classify_work_type(
            _ann(title="LMS 고도화", proc_class="정보시스템개발서비스"), wt
        )
        == "개발"
    )
    assert (
        stages.classify_work_type(
            _ann(title="AI 부트캠프 위탁운영", proc_class="기타교육서비스"), wt
        )
        == "교육운영"
    )
    assert (
        stages.classify_work_type(
            _ann(title="가로수 전지 용역", proc_large="폐기물 처리 및 재활용서비스"), wt
        )
        == "무관"
    )


# --- sources 캐시 (S0) ---


def test_collect_caches_raw_per_window(monkeypatch, tmp_path):
    from service.edu_bid import sources

    monkeypatch.setattr(sources, "_CACHE_DIR", tmp_path)
    calls = {"n": 0}

    def fake_paginate(fetch_fn, kind, bgn, end, session):
        calls["n"] += 1
        return [{"bidNtceNo": "R1", "bidNtceNm": "코딩교육 플랫폼"}]

    monkeypatch.setattr(sources, "_paginate", fake_paginate)

    class _KnSrc:
        enabled_sources = [{"adapter": "g2b", "kind": "servc", "id": "g2b_servc"}]

    win = ("202605290000", "202605292359")
    first = sources.collect(_KnSrc(), win)
    second = sources.collect(_KnSrc(), win)  # 캐시 적중 → API 미호출
    assert calls["n"] == 1  # 두 번째는 캐시
    assert first[0].title == second[0].title == "코딩교육 플랫폼"


# --- knowledge 로딩 (실파일) ---


def test_load_knowledge_real_files():
    kn = load_knowledge("dev")
    assert len(kn.assets) > 0
    assert "reuse" in kn.weights
    assert any(s["adapter"] == "g2b" for s in kn.shared.enabled_sources)


def test_each_track_loads_own_scoring_with_shared_source():
    dev = load_knowledge("dev")
    content = load_knowledge("content")
    edu = load_knowledge("edu")
    # 공유 지식(역량·자격·소스·사업유형)은 트랙끼리 내용 동일
    assert dev.shared == content.shared
    assert dev.shared == edu.shared
    # 전략(점수정책)은 트랙마다 다른 문서
    descs = {
        k.scoring_policy["strategy"]["primary"]["desc"] for k in (dev, content, edu)
    }
    assert len(descs) == 3


def test_assets_are_filtered_by_track_tag():
    dev = load_knowledge("dev").assets
    content = load_knowledge("content").assets
    edu = load_knowledge("edu").assets
    dev_ids = {a["id"] for a in dev}
    content_ids = {a["id"] for a in content}
    # 트랙뷰는 서로 다르고, 콘텐츠 트랙이 가장 좁다
    assert dev_ids != content_ids
    assert len(content) < len(dev)
    # 태그대로 노출: 콘텐츠 저작은 콘텐츠에, 인프라는 콘텐츠에서 빠진다
    assert "course_authoring" in content_ids
    assert "gov_cloud_infra" in dev_ids and "gov_cloud_infra" not in content_ids
    # 모든 트랙뷰 자산은 자기 트랙 태그를 가진다
    assert all("edu" in a.get("tracks", []) for a in edu)


def test_track_performance_is_per_track_and_empty():
    # 구조만 있고 비어 있음 — 평가 프롬프트의 실적상태는 '약함'으로 나온다
    for key in ("dev", "content", "edu"):
        assert load_knowledge(key).track_performance == []


# --- run_track (사업유형 분기, LLM 모킹) ---


def test_run_track_filters_by_work_type(monkeypatch):
    from service.edu_bid import pipeline, evaluate
    from service.edu_bid.schemas import GateResult

    # LLM 평가는 모킹 — run_track 의 사업유형 분기만 검증한다.
    def fake_eval(cands, kn, model, batch_size, llm=None):
        return {
            i: EvalOut(
                index=i,
                axes=Axes(reuse=80, winnability=70, value=60, performance_building=50),
                quant_barrier="low",
                wired_risk="low",
                matched_assets=["x"],
                rationale="r",
            )
            for i in range(len(cands))
        }

    monkeypatch.setattr(evaluate, "evaluate", fake_eval)

    kn = load_knowledge("content")
    a_dev = _ann(title="LMS 고도화")
    a_dev.work_type = "개발"
    a_content = _ann(title="디지털 교재 제작")
    a_content.work_type = "콘텐츠"
    gated = [(a_dev, ["x"], GateResult("pass")), (a_content, ["x"], GateResult("pass"))]

    decisions = pipeline.run_track(
        "콘텐츠 제작",
        ["콘텐츠"],
        gated,
        kn,
        "gpt-x",
        batch_size=10,
        do_enrich=False,
    )
    # 콘텐츠 트랙은 사업유형 '콘텐츠' 1건만 평가 (개발 건은 제외)
    assert len(decisions) == 1
    assert decisions[0].announcement.title == "디지털 교재 제작"
