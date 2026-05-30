"""
파이프라인 순수 단계 함수 (네트워크·LLM 없음)

S1 정규화/dedupe, S2 게이트, S3 트리아지, S6 결정, S7 보고.
모두 입력→출력 결정적 변환이라 단위 테스트가 쉽다.
"""

from datetime import date, datetime, time, timedelta

from .schemas import Announcement, GateResult, Decision

# --- 공통 ---


def _first(item: dict, *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def build_window(today: date, lookback_days: int) -> tuple[str, str]:
    """[today - lookback_days, today) 게시일 구간을 YYYYMMDDHHMM 으로."""
    end_dt = datetime.combine(today, time.min)
    bgn_dt = end_dt - timedelta(days=lookback_days)
    end_dt = end_dt - timedelta(minutes=1)
    fmt = "%Y%m%d%H%M"
    return bgn_dt.strftime(fmt), end_dt.strftime(fmt)


# --- S1 정규화 ---


def to_announcement(
    item: dict, source: str, kind: str, kind_label: str
) -> Announcement:
    """G2B raw item → Announcement (필드 누락에 견고하게 폴백)."""
    spec_docs = [
        {"name": item.get(f"ntceSpecFileNm{i}", ""), "url": item[f"ntceSpecDocUrl{i}"]}
        for i in range(1, 11)
        if item.get(f"ntceSpecDocUrl{i}")
    ]
    return Announcement(
        source=source,
        kind=kind,
        kind_label=kind_label,
        bid_no=_first(item, "bidNtceNo", "bidno"),
        bid_ord=_first(item, "bidNtceOrd"),
        title=_first(item, "bidNtceNm"),
        notice_inst=_first(item, "ntceInsttNm"),
        demand_inst=_first(item, "dminsttNm"),
        notice_dt=_first(item, "bidNtceDt", "bidNtceDate"),
        close_dt=_first(item, "bidClseDt", "bidClseDate"),
        opening_dt=_first(item, "opengDt", "opengDate"),
        estimated_price=_first(item, "presmptPrce"),
        budget_amt=_first(item, "asignBdgtAmt"),
        url=_first(item, "bidNtceDtlUrl", "bidNtceUrl"),
        award_method=_first(item, "sucsfbidMthdNm"),
        contract_method=_first(item, "cntrctCnclsMthdNm"),
        re_notice=_first(item, "reNtceYn"),
        result_competition=_first(item, "arsltCmptYn"),
        industry_limit=_first(item, "indstrytyLmtYn"),
        region_limit_basis=_first(item, "rgnLmtBidLocplcJdgmBssNm"),
        tech_eval_rate=_first(item, "techAbltEvlRt"),
        price_eval_rate=_first(item, "bidPrceEvlRt"),
        info_biz=_first(item, "infoBizYn"),
        service_div=_first(item, "srvceDivNm"),
        proc_class=_first(item, "pubPrcrmntClsfcNm"),
        proc_mid=_first(item, "pubPrcrmntMidClsfcNm"),
        proc_large=_first(item, "pubPrcrmntLrgClsfcNm"),
        spec_docs=spec_docs,
        raw=item,
    )


def dedupe_by_notice(anns: list[Announcement]) -> list[Announcement]:
    """같은 공고번호의 여러 차수는 최신 차수만 남긴다."""
    latest: dict[str, Announcement] = {}
    passthrough: list[Announcement] = []
    for a in anns:
        if not a.bid_no:
            passthrough.append(a)
            continue
        ord_val = int(a.bid_ord) if a.bid_ord.isdigit() else -1
        prev = latest.get(a.bid_no)
        if prev is None or ord_val >= (
            int(prev.bid_ord) if prev.bid_ord.isdigit() else -1
        ):
            latest[a.bid_no] = a
    return list(latest.values()) + passthrough


# --- S2 게이트 (목록 단계 정량 신호 기반) ---


def gate(ann: Announcement, eligibility: dict) -> GateResult:
    """참가 가능성 판정. 목록 단계에서 가능한 신호만 사용.

    - 실적경쟁(arsltCmptYn=Y): 실적제한 존재 → 현 실적 약하면 near_miss.
    - 지역제한/업종제한: 존재 시 reason 으로 표시(상세 확인 필요), 하드 fail 보류.
    """
    # 참가 불가(하드 제외): 수의시담/다자간수의시담은 계약대상자가 이미 정해진
    # 정보공개용 공고라 외부 업체는 참가할 수 없다. (소액수의견적=견적경쟁은 참가 가능)
    if "수의시담" in ann.award_method:
        return GateResult(
            status="fail",
            reasons=["수의시담/다자간수의시담 — 계약대상자 외 참가 불가(정보공개용)"],
        )

    reasons: list[str] = []
    status = "pass"

    perf = eligibility.get("performance", []) or []
    weak_performance = len(perf) == 0  # 원장이 비면 정량 실적 약함

    if ann.result_competition == "Y":
        if weak_performance:
            status = "near_miss"
            reasons.append("실적제한경쟁(실적신청 필요) — 현 정량 실적 약함")
        else:
            reasons.append("실적제한경쟁 — 보유 실적 충족 여부 확인 필요")

    if ann.region_limit_basis:
        reasons.append(
            f"지역제한 있음({ann.region_limit_basis}) — 소재지 적합 확인 필요(S4)"
        )

    if ann.industry_limit == "Y":
        reasons.append("업종제한 있음 — 보유 업종/직생 적합 확인 필요(S4)")

    return GateResult(status=status, reasons=reasons)


# --- S3 트리아지 (역량 키워드 매칭) ---


def build_keyword_index(capability_profile: dict) -> dict[str, list[str]]:
    """asset id → 키워드 리스트."""
    return {
        a["id"]: a.get("keywords", []) for a in capability_profile.get("assets", [])
    }


def triage(ann: Announcement, keyword_index: dict[str, list[str]]) -> list[str]:
    """제목·기관·분류명에 우리 자산 키워드가 걸리는지. 매칭된 asset id 리스트 반환."""
    haystack = " ".join(
        [ann.title, ann.demand_inst, ann.notice_inst, ann.proc_class, ann.service_div]
    )
    matched: list[str] = []
    for asset_id, keywords in keyword_index.items():
        if any(kw and kw in haystack for kw in keywords):
            matched.append(asset_id)
    return matched


# --- S3.5 사업유형 분류 ---


def classify_work_type(ann: Announcement, work_types: dict) -> str:
    """조달 품목분류 + 제목 키워드로 사업유형을 태깅한다.

    order 순서대로, 각 유형에서 제목 키워드(우선) → 조달분류명(소/중/대) 매칭.
    """
    rules = work_types.get("rules", {})
    proc_text = " ".join([ann.proc_class, ann.proc_mid, ann.proc_large])
    for wt in work_types.get("order", []):
        if any(kw in ann.title for kw in rules.get(wt, {}).get("title", [])):
            return wt
    for wt in work_types.get("order", []):
        if any(p in proc_text for p in rules.get(wt, {}).get("proc", [])):
            return wt
    return work_types.get("default", "기타")


# --- S6 결정 ---


def _price_won(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def decide(
    ann: Announcement,
    gate_result: GateResult,
    axes: dict,
    quant_barrier: str,
    wired_risk: str,
    matched_assets: list[str],
    rationale: str,
    knowledge,
) -> Decision:
    """4축 가중합으로 종합점수·라벨 산출."""
    weights = knowledge.weights
    score = sum(float(axes.get(k, 0)) * float(weights.get(k, 0)) for k in weights)

    th = knowledge.thresholds
    if gate_result.status == "fail":
        label = "제외"
    elif gate_result.status == "near_miss":
        label = "미래타깃"
    elif score >= th.get("recommend", 70):
        label = "입찰추천"
    elif score >= th.get("review", 50):
        label = "검토"
    else:
        label = "제외"

    return Decision(
        announcement=ann,
        gate=gate_result,
        axes=axes,
        quant_barrier=quant_barrier,
        matched_assets=matched_assets,
        score=round(score, 1),
        label=label,
        rationale=rationale,
        wired_risk=wired_risk,
    )


# --- S7 보고 ---

_REPORT_LABELS = ["입찰추천", "검토", "미래타깃"]


def format_report(decisions: list[Decision], window: tuple[str, str]) -> str:
    """보고 대상(추천/검토/미래타깃)을 라벨·점수 순으로 Slack 텍스트화."""
    bgn = window[0]
    bgn_disp = f"{bgn[:4]}-{bgn[4:6]}-{bgn[6:8]}"
    shown = [d for d in decisions if d.label in _REPORT_LABELS]
    shown.sort(key=lambda d: (_REPORT_LABELS.index(d.label), -d.score))

    lines = [f":mega: 교육 외주 입찰 후보 {len(shown)}건 (게시일 {bgn_disp} 구간)", ""]
    for d in shown:
        a = d.announcement
        price = _price_won(a.estimated_price)
        price_disp = (
            f"{price:,}원" if price is not None else (a.estimated_price or "미상")
        )
        assets = ", ".join(d.matched_assets) if d.matched_assets else "-"
        tag = " :page_facing_up:정독" if d.enriched else ""
        lines.append(f"*[{d.label}·{d.score}점]{tag} {a.title}* ({a.kind_label})")
        lines.append(
            f" · 수요기관: {a.demand_inst or a.notice_inst or '미상'} | 추정가격: {price_disp} | 마감: {a.close_dt or '미상'}"
        )
        lines.append(
            f" · 사업유형: {a.work_type or '?'} | 축: 재사용 {d.axes.get('reuse')} / 수주 {d.axes.get('winnability')}"
            f" / 가치 {d.axes.get('value')} / 실적적립 {d.axes.get('performance_building')}"
            f" | 정량장벽 {d.quant_barrier} | 내정위험 {d.wired_risk} | 낙찰 {a.award_method or '미상'}"
        )
        lines.append(f" · 자산: {assets}")
        lines.append(f" · 근거: {d.rationale}")
        if d.gate.reasons:
            lines.append(f" · 게이트: {'; '.join(d.gate.reasons)}")
        if a.url:
            lines.append(f" · {a.url}")
        lines.append("")
    return "\n".join(lines).rstrip()
