"""
파이프라인 순수 단계 함수 (네트워크·LLM 없음)

S1 정규화/dedupe, S2 게이트, S3 트리아지, S6 결정, S7 보고.
모두 입력→출력 결정적 변환이라 단위 테스트가 쉽다.
"""

from datetime import datetime, timedelta

from .schemas import (
    Announcement,
    GateResult,
    Decision,
    EvalOut,
    LABEL_RECOMMEND,
    LABEL_REVIEW,
    LABEL_FUTURE,
    LABEL_EXCLUDE,
    REPORTABLE_LABELS,
)

# --- 공통 ---


def _first(item: dict, *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def build_incremental_window(now: datetime) -> tuple[str, str]:
    """직전 실행 이후 게시분을 보는 무상태 구간.

    [어제 now 시각, now] 을 YYYYMMDDHHMM 으로 반환한다.
    매일 같은 시각에 돌면 구간이 빈틈·중복 없이 이어진다(주말·공휴일 구분 없음).
    """
    bgn_dt = now - timedelta(days=1)
    fmt = "%Y%m%d%H%M"
    return bgn_dt.strftime(fmt), now.strftime(fmt)


# --- S1 정규화 ---


def to_announcement(item: dict, kind_label: str) -> Announcement:
    """G2B raw item → Announcement (필드 누락에 견고하게 폴백)."""
    spec_docs = [
        {"name": item.get(f"ntceSpecFileNm{i}", ""), "url": item[f"ntceSpecDocUrl{i}"]}
        for i in range(1, 11)
        if item.get(f"ntceSpecDocUrl{i}")
    ]
    return Announcement(
        kind_label=kind_label,
        bid_no=_first(item, "bidNtceNo", "bidno"),
        bid_ord=_first(item, "bidNtceOrd"),
        title=_first(item, "bidNtceNm"),
        notice_inst=_first(item, "ntceInsttNm"),
        demand_inst=_first(item, "dminsttNm"),
        close_dt=_first(item, "bidClseDt", "bidClseDate"),
        estimated_price=_first(item, "presmptPrce"),
        url=_first(item, "bidNtceDtlUrl", "bidNtceUrl"),
        award_method=_first(item, "sucsfbidMthdNm"),
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
    )


def to_announcement_prespec(item: dict, kind_label: str) -> Announcement:
    """사전규격 raw item → Announcement (본공고와 필드가 달라 별도 매핑).

    사전규격은 공고 전 단계라 낙찰방식·실적경쟁 등은 없다. 대신 의견등록 마감(영업 윈도우)과
    규격서 파일을 제공한다. 세부품명(prdctDtlList)을 proc_class 로 써서 work_type 분류에 활용.
    """
    spec_docs = [
        {"name": f"규격서{i}", "url": item[f"specDocFileUrl{i}"]}
        for i in range(1, 6)
        if item.get(f"specDocFileUrl{i}")
    ]
    # prdctDtlList: "[순번^물품분류번호^세부품명]" (다건 가능) → 첫 세부품명
    detail = _first(item, "prdctDtlList")
    proc_class = ""
    if "^" in detail:
        parts = detail.strip("[]").split("]")[0].split("^")
        if len(parts) >= 3:
            proc_class = parts[2]
    opinion_close = _first(item, "opninRgstClseDt")
    return Announcement(
        kind_label=kind_label,
        bid_no=_first(item, "bfSpecRgstNo"),
        bid_ord="0",
        title=_first(item, "prdctClsfcNoNm"),
        notice_inst=_first(item, "orderInsttNm"),
        demand_inst=_first(item, "rlDminsttNm"),
        close_dt=opinion_close,
        estimated_price=_first(item, "asignBdgtAmt"),
        url=spec_docs[0]["url"] if spec_docs else "",
        info_biz=_first(item, "swBizObjYn"),
        service_div=_first(item, "bsnsDivNm"),
        proc_class=proc_class,
        stage="presearch",
        opinion_close_dt=opinion_close,
        spec_docs=spec_docs,
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
    # 참가 불가(하드 제외): 계약대상자가 이미 정해진 정보공개용 공고는 외부 업체가
    # 참가할 수 없다. 제외 대상 낙찰방식은 원장(excluded_award_methods)에서 관리한다.
    for method in eligibility.get("excluded_award_methods", []):
        if method in ann.award_method:
            return GateResult(
                status="fail",
                reasons=[f"{method} — 계약대상자 외 참가 불가(정보공개용)"],
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


def decide(
    ann: Announcement,
    gate_result: GateResult,
    ev: EvalOut,
    fallback_assets: list[str],
    knowledge,
) -> Decision:
    """LLM 평가(EvalOut)를 4축 가중합으로 종합점수·라벨화해 최종 결정으로 변환.

    게이트 통과분(pass/near_miss)만 들어온다 — fail 은 prepare 에서 이미 제외된다.
    """
    axes = ev.axes.model_dump()
    weights = knowledge.weights
    score = sum(float(axes.get(k, 0)) * float(weights.get(k, 0)) for k in weights)

    th = knowledge.thresholds
    if score >= th.get("recommend", 70):
        label = LABEL_RECOMMEND
    elif score >= th.get("review", 50):
        label = LABEL_REVIEW
    else:
        label = LABEL_EXCLUDE

    # 실적제한경쟁(near_miss)은 실적장벽으로 지금은 못 잡는 건 — 적합도가 보고 문턱(추천/검토)
    # 을 넘는 건만 '미래타깃'으로 낮춰 추적한다. 문턱 미만(무관·저적합)은 실적경쟁이어도 제외해,
    # 재사용 0짜리 공고가 near_miss 라는 이유만으로 보고되지 않게 한다.
    if gate_result.status == "near_miss" and label in (LABEL_RECOMMEND, LABEL_REVIEW):
        label = LABEL_FUTURE

    return Decision(
        announcement=ann,
        gate=gate_result,
        axes=axes,
        quant_barrier=ev.quant_barrier,
        matched_assets=ev.matched_assets or fallback_assets,
        score=round(score, 1),
        label=label,
        rationale=ev.rationale,
        wired_risk=ev.wired_risk,
    )


# --- S7 보고 ---


def format_won(value: str) -> str:
    """추정가격/예산 문자열 → '1,234,000원' 또는 원문(미상)."""
    return f"{int(value):,}원" if value.isdigit() else (value or "미상")


def format_report(
    decisions: list[Decision], window: tuple[str, str], track_name: str
) -> str:
    """보고 대상(추천/검토/미래타깃)을 라벨·점수 순으로 Slack 텍스트화."""
    bgn = window[0]
    bgn_disp = f"{bgn[:4]}-{bgn[4:6]}-{bgn[6:8]}"
    shown = [d for d in decisions if d.label in REPORTABLE_LABELS]
    shown.sort(key=lambda d: (REPORTABLE_LABELS.index(d.label), -d.score))

    lines = [
        f":mega: [{track_name}] 입찰 후보 {len(shown)}건 (게시일 {bgn_disp} 구간)",
        "",
    ]
    for d in shown:
        a = d.announcement
        price_disp = format_won(a.estimated_price)
        assets = ", ".join(d.matched_assets) if d.matched_assets else "-"
        tag = " :page_facing_up:정독" if d.enriched else ""
        stage_tag = " :hourglass:사전규격" if a.stage == "presearch" else ""
        lines.append(
            f"*[{d.label}·{d.score}점]{tag}{stage_tag} {a.title}* ({a.kind_label})"
        )
        deadline = (
            f"의견마감: {a.opinion_close_dt or '미상'}"
            if a.stage == "presearch"
            else f"마감: {a.close_dt or '미상'}"
        )
        lines.append(
            f" · 수요기관: {a.demand_inst or a.notice_inst or '미상'} | 추정가격: {price_disp} | {deadline}"
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
