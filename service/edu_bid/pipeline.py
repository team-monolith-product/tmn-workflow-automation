"""
오케스트레이터 — 단계를 순서대로 호출하는 얇은 조립부.

S0 수집 → S1 정규화/dedupe → S2 게이트 → S3 트리아지 → S5 평가 → S6 결정 → S7 보고.
S4 보강(규격서·실적/지역 상세)은 후속. 현재는 목록 단계 신호로 판정.
"""

from datetime import date

from .knowledge import load_knowledge
from . import sources, stages, evaluate
from .schemas import Decision


def run(
    *,
    model: str,
    lookback_days: int,
    batch_size: int,
    today: date,
    dry_run: bool,
    limit: int | None = None,
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

    print(f"[edu-bid] 라벨 분포: {dict(Counter(d.label for d in decisions))}")
    return decisions
