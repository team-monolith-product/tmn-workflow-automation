"""
오케스트레이터 — 단계를 순서대로 호출하는 얇은 조립부.

S0 수집 → S1 정규화/dedupe → S2 게이트 → S3 트리아지 → S5 평가 → S6 결정 → S7 보고.
S4 보강(규격서·실적/지역 상세)은 후속. 현재는 목록 단계 신호로 판정.
"""

from datetime import date

from .knowledge import load_knowledge
from . import sources, stages, evaluate, enrich
from .schemas import Decision

_SHORTLIST_LABELS = ("입찰추천", "검토", "미래타깃")


def run(
    *,
    model: str,
    lookback_days: int,
    batch_size: int,
    today: date,
    dry_run: bool,
    limit: int | None = None,
    do_enrich: bool = True,
    session=None,
    knowledge=None,
) -> list[Decision]:
    kn = knowledge or load_knowledge()
    window = stages.build_window(today, lookback_days)
    print(
        f"[edu-bid] 구간 {window[0]}~{window[1]} / 소스 {[s['id'] for s in kn.enabled_sources]}"
    )

    # S0 수집 + S1 정규화/dedupe
    anns = sources.collect(kn, window, session=session)
    anns = stages.dedupe_by_notice(anns)
    print(f"[edu-bid] 수집·dedupe: {len(anns)}건")

    # S3 트리아지 (역량 키워드) — 비용 깔때기
    kw_index = stages.build_keyword_index(kn.capability_profile)
    candidates: list[tuple] = []
    for a in anns:
        matched = stages.triage(a, kw_index)
        if matched:
            candidates.append((a, matched))
    print(f"[edu-bid] 트리아지 통과: {len(candidates)}건")

    # S2 게이트 — 참가 불가(fail)는 평가 전에 제외해 LLM 비용 절감.
    # near_miss/pass 만 평가 대상으로 넘긴다.
    eligibility = kn.eligibility_ledger
    decisions: list[Decision] = []  # 사전 제외분 포함
    gated: list[tuple] = []  # (ann, matched, gate)
    for ann, matched in candidates:
        g = stages.gate(ann, eligibility)
        if g.status == "fail":
            decisions.append(
                Decision(
                    announcement=ann,
                    gate=g,
                    axes={},
                    quant_barrier="n/a",
                    matched_assets=matched,
                    score=0.0,
                    label="제외",
                    rationale=g.reasons[0] if g.reasons else "게이트 탈락",
                )
            )
        else:
            gated.append((ann, matched, g))
    print(
        f"[edu-bid] 게이트 제외(참가불가): {len(decisions)}건 / 평가대상: {len(gated)}건"
    )

    if limit is not None:
        gated = gated[:limit]
        print(f"[edu-bid] --limit 적용: {len(gated)}건만 평가")
    if not gated:
        print("[edu-bid] 평가 대상 없음. 종료.")
        return decisions

    # S5 평가 + S6 결정 (게이트는 이미 계산됨)
    evals = evaluate.evaluate([(a, m) for a, m, _ in gated], kn, model, batch_size)
    for i, (ann, matched, g) in enumerate(gated):
        ev = evals.get(i)
        if ev is None:
            continue
        decisions.append(
            stages.decide(
                ann,
                g,
                ev.axes.model_dump(),
                ev.quant_barrier,
                ev.matched_assets or matched,
                ev.rationale,
                kn,
            )
        )

    from collections import Counter

    print(f"[edu-bid] 1차 라벨 분포: {dict(Counter(d.label for d in decisions))}")

    # S4 보강 + 심층 재평가 — 숏리스트(추천/검토/미래타깃)만 규격서 정독
    if do_enrich:
        shortlist = [d for d in decisions if d.label in _SHORTLIST_LABELS]
        print(f"[edu-bid] S4 정독 대상: {len(shortlist)}건")
        for d in shortlist:
            if not d.announcement.spec_docs:
                continue
            spec_text = enrich.enrich(d.announcement, session=session)
            if not spec_text:
                continue
            ev = evaluate.evaluate_deep(
                d.announcement, d.matched_assets, spec_text, kn, model
            )
            gate_result = stages.gate(d.announcement, eligibility)
            deep = stages.decide(
                d.announcement,
                gate_result,
                ev.axes.model_dump(),
                ev.quant_barrier,
                ev.matched_assets or d.matched_assets,
                ev.rationale,
                kn,
            )
            deep.enriched = True
            decisions[decisions.index(d)] = deep
        print(f"[edu-bid] 최종 라벨 분포: {dict(Counter(d.label for d in decisions))}")

    return decisions
