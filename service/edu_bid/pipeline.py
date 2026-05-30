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

    if limit is not None:
        candidates = candidates[:limit]
        print(f"[edu-bid] --limit 적용: {len(candidates)}건만 평가")
    if not candidates:
        print("[edu-bid] 후보 없음. 종료.")
        return []

    # S5 평가
    evals = evaluate.evaluate(candidates, kn, model, batch_size)

    # S2 게이트 + S6 결정
    eligibility = kn.eligibility_ledger
    decisions: list[Decision] = []
    for i, (ann, matched) in enumerate(candidates):
        ev = evals.get(i)
        if ev is None:
            continue
        gate_result = stages.gate(ann, eligibility)
        decisions.append(
            stages.decide(
                ann,
                gate_result,
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
