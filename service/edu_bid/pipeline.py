"""
오케스트레이터 — 단계를 순서대로 호출하는 얇은 조립부.

S0 수집 → S1 정규화/dedupe → S2 게이트 → S3 트리아지 → S5 평가 → S6 결정 → S7 보고.
S4 보강(규격서·실적/지역 상세)은 후속. 현재는 목록 단계 신호로 판정.
"""

from .knowledge import load_knowledge
from . import sources, stages, evaluate, enrich
from .schemas import Decision, LABEL_EXCLUDE, REPORTABLE_LABELS


def run(
    *,
    model: str,
    batch_size: int,
    window: tuple[str, str],
    limit: int | None = None,
    do_enrich: bool = True,
    use_cache: bool = True,
    session=None,
    knowledge=None,
) -> list[Decision]:
    kn = knowledge or load_knowledge()
    print(
        f"[edu-bid] 구간 {window[0]}~{window[1]} / 소스 {[s['id'] for s in kn.enabled_sources]}"
    )

    # S0 수집 + S1 정규화/dedupe
    anns = sources.collect(kn, window, session=session, use_cache=use_cache)
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

    # S3.5 사업유형 분류 + 무관 유형 사전 제외(비용 절감)
    drop_types = set(kn.work_types.get("drop_for_eval", []))
    kept: list[tuple] = []
    dropped_wt = 0
    for a, matched in candidates:
        a.work_type = stages.classify_work_type(a, kn.work_types)
        if a.work_type in drop_types:
            dropped_wt += 1
        else:
            kept.append((a, matched))
    candidates = kept
    print(
        f"[edu-bid] 사업유형 무관 제외: {dropped_wt}건 / 남은 후보: {len(candidates)}건"
    )

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
                    label=LABEL_EXCLUDE,
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
                ev.wired_risk,
                ev.matched_assets or matched,
                ev.rationale,
                kn,
            )
        )

    from collections import Counter

    print(f"[edu-bid] 1차 라벨 분포: {dict(Counter(d.label for d in decisions))}")

    # S4 보강 + 심층 재평가 — 숏리스트(추천/검토/미래타깃)만 규격서 정독
    if do_enrich:
        shortlist_idx = [
            i for i, d in enumerate(decisions) if d.label in REPORTABLE_LABELS
        ]
        print(f"[edu-bid] S4 정독 대상: {len(shortlist_idx)}건")
        for i in shortlist_idx:
            d = decisions[i]
            if not d.announcement.spec_docs:
                continue
            spec_text = enrich.enrich(d.announcement, session=session)
            if not spec_text:
                continue
            ev = evaluate.evaluate_deep(
                d.announcement, d.matched_assets, spec_text, kn, model
            )
            deep = stages.decide(
                d.announcement,
                d.gate,  # 게이트는 결정적 — 1차에서 계산한 결과 재사용
                ev.axes.model_dump(),
                ev.quant_barrier,
                ev.wired_risk,
                ev.matched_assets or d.matched_assets,
                ev.rationale,
                kn,
            )
            deep.enriched = True
            decisions[i] = deep
        print(f"[edu-bid] 최종 라벨 분포: {dict(Counter(d.label for d in decisions))}")

    return decisions
