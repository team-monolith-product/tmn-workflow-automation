"""
오케스트레이터 — 단계를 순서대로 호출하는 얇은 조립부.

공유 상단부(prepare): S0 수집 → S1 정규화/dedupe → S3.5 사업유형 분류 → S3 트리아지
→ S2 게이트. 트랙 무관(공유 지식만 사용)으로 한 번만 돈다. 후보 채택은 역량 키워드
매칭과 담당 사업유형 분류의 합집합이다(둘 중 하나만 걸려도 채택).
트랙 하단부(run_track): 사업유형으로 후보를 갈라 트랙 지식으로 S5 평가 → S6 결정 →
S4 정독 재평가. 트랙마다 다른 전략·점수정책·채널이 여기서 갈린다. 사업유형이 트랙을
겹치지 않게 나누므로, 각 공고는 정확히 한 트랙에서 한 번만 평가된다(비용 1배).
"""

from collections import Counter

from . import sources, stages, evaluate, enrich
from .schemas import Announcement, GateResult, Decision, REPORTABLE_LABELS

# 게이트 통과(또는 near_miss) 후보 — 트랙으로 분기되기 전 공유 단위.
GatedCandidate = tuple[Announcement, list[str], GateResult]


def prepare(
    window: tuple[str, str],
    shared,
    in_scope_work_types: set[str],
    *,
    limit: int | None = None,
    session=None,
    use_cache: bool = True,
) -> list[GatedCandidate]:
    """공유 상단부. 수집·분류·트리아지·게이트까지 트랙 무관하게 한 번 돈다.

    shared 는 SharedKnowledge(역량·자격·소스·사업유형) — 트랙 전략·점수정책은 여기서 안 쓴다.
    in_scope_work_types 는 어느 트랙이든 담당하는 사업유형 집합(config 의 tracks 에서 옴).
    후보 채택은 합집합이다: 역량 키워드가 걸리거나(제품 호명), 담당 사업유형으로 분류되면
    남긴다. 연수·역량강화처럼 우리 기술을 직접 호명하지 않는 교육운영 공고가 키워드 트리아지
    단독 게이트에서 탈락하던 문제를 막는다(실제 적합도는 뒷단 LLM 평가의 reuse 축이 거른다).
    반환: 게이트 통과(pass/near_miss) 후보 [(ann, matched_assets, gate)].
    ann.work_type 이 채워진 상태로 넘어가 run_track 에서 트랙으로 분기된다.
    """
    print(
        f"[edu-bid] 구간 {window[0]}~{window[1]} / 소스 {[s['id'] for s in shared.enabled_sources]}"
    )

    # S0 수집 + S1 정규화/dedupe
    anns = sources.collect(shared, window, session=session, use_cache=use_cache)
    anns = stages.dedupe_by_notice(anns)
    print(f"[edu-bid] 수집·dedupe: {len(anns)}건")

    # S3.5 사업유형 분류(무비용) → S3 트리아지(역량 키워드)와 합집합으로 후보 채택.
    # 무관 유형은 분류 즉시 제외(LLM 비용 절감).
    kw_index = stages.build_keyword_index(shared.capability_profile)
    drop_types = set(shared.work_types.get("drop_for_eval", []))
    candidates: list[tuple] = []
    dropped_wt = 0
    for a in anns:
        a.work_type = stages.classify_work_type(a, shared.work_types)
        if a.work_type in drop_types:
            dropped_wt += 1
            continue
        matched = stages.triage(a, kw_index)
        if matched or a.work_type in in_scope_work_types:
            candidates.append((a, matched))
    print(
        f"[edu-bid] 사업유형 무관 제외: {dropped_wt}건 / 후보(키워드∪담당유형): {len(candidates)}건"
    )

    # S2 게이트 — 참가 불가(fail)는 평가 전에 제외해 LLM 비용 절감.
    # near_miss/pass 만 평가 대상으로 넘긴다(near_miss 는 결정 단계에서 미래타깃 처리).
    gated: list[GatedCandidate] = []
    dropped_gate = 0
    for ann, matched in candidates:
        g = stages.gate(ann, shared.eligibility_ledger)
        if g.status == "fail":
            dropped_gate += 1
        else:
            gated.append((ann, matched, g))
    print(
        f"[edu-bid] 게이트 제외(참가불가): {dropped_gate}건 / 평가대상: {len(gated)}건"
    )

    if limit is not None:
        gated = gated[:limit]
        print(f"[edu-bid] --limit 적용: {len(gated)}건만 평가")
    return gated


def run_track(
    track_name: str,
    work_types: list[str],
    gated: list[GatedCandidate],
    kn,
    model: str,
    batch_size: int,
    *,
    do_enrich: bool = True,
    session=None,
) -> list[Decision]:
    """트랙 하단부. 사업유형이 이 트랙에 속한 후보만 트랙 지식으로 평가·결정·정독한다."""
    wt = set(work_types)
    subset = [(a, m, g) for a, m, g in gated if a.work_type in wt]
    print(f"[edu-bid][{track_name}] 사업유형 {work_types} 후보: {len(subset)}건")
    if not subset:
        return []

    # S5 평가 + S6 결정 (게이트는 prepare 에서 계산됨)
    evals = evaluate.evaluate([(a, m) for a, m, _ in subset], kn, model, batch_size)
    decisions: list[Decision] = []
    for i, (ann, matched, g) in enumerate(subset):
        ev = evals.get(i)
        if ev is None:
            continue
        decisions.append(stages.decide(ann, g, ev, matched, kn))
    print(
        f"[edu-bid][{track_name}] 1차 라벨 분포: {dict(Counter(d.label for d in decisions))}"
    )

    # S4 보강 + 심층 재평가 — 숏리스트(추천/검토/미래타깃)만 규격서 정독
    if do_enrich:
        shortlist_idx = [
            i for i, d in enumerate(decisions) if d.label in REPORTABLE_LABELS
        ]
        print(f"[edu-bid][{track_name}] S4 정독 대상: {len(shortlist_idx)}건")
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
            deep = stages.decide(d.announcement, d.gate, ev, d.matched_assets, kn)
            deep.enriched = True
            decisions[i] = deep
        print(
            f"[edu-bid][{track_name}] 최종 라벨 분포: {dict(Counter(d.label for d in decisions))}"
        )

    return decisions
