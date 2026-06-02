"""
오케스트레이터 — 단계를 순서대로 호출하는 얇은 조립부.

공유 상단부(prepare): S0 수집 → S1 정규화/dedupe → S3 트리아지 → S3.5 사업유형 분류
→ S2 게이트. 트랙 무관(공유 지식만 사용)으로 한 번만 돈다.
트랙 하단부(run_track): 사업유형으로 후보를 갈라 트랙 지식으로 S5 평가 → S6 결정 →
S4 정독 재평가. 트랙마다 다른 전략·점수정책·채널이 여기서 갈린다. 사업유형이 트랙을
겹치지 않게 나누므로, 각 공고는 정확히 한 트랙에서 한 번만 평가된다(비용 1배).
"""

from collections import Counter
from dataclasses import dataclass, field

from . import sources, stages, evaluate, enrich
from .schemas import Announcement, GateResult, Decision, REPORTABLE_LABELS

# 게이트 통과(또는 near_miss) 후보 — 트랙으로 분기되기 전 공유 단위.
GatedCandidate = tuple[Announcement, list[str], GateResult]


@dataclass
class PrepareResult:
    """공유 상단부 산출물 — 게이트 통과 후보 + 깔때기 카운트(보고·DB 적재·디버그용)."""

    gated: list[GatedCandidate] = field(default_factory=list)
    collected: int = 0  # 수집·dedupe 후
    triaged: int = 0  # 트리아지 통과
    dropped_work_type: int = 0  # 무관 사업유형 제외
    dropped_gate: int = 0  # 게이트(참가불가) 제외


def prepare(
    window: tuple[str, str],
    knowledge,
    *,
    limit: int | None = None,
    session=None,
    use_cache: bool = True,
) -> PrepareResult:
    """공유 상단부. 수집·트리아지·사업유형·게이트까지 트랙 무관하게 한 번 돈다.

    knowledge 는 공유 지식(역량·자격·소스·사업유형)만 읽으므로 아무 트랙 것이나 무방하다.
    반환: PrepareResult(게이트 통과 후보 + 깔때기 카운트).
    각 후보의 ann.work_type 이 채워진 상태로 넘어가 run_track 에서 트랙으로 분기된다.
    """
    kn = knowledge
    print(
        f"[edu-bid] 구간 {window[0]}~{window[1]} / 소스 {[s['id'] for s in kn.enabled_sources]}"
    )

    # S0 수집 + S1 정규화/dedupe
    anns = sources.collect(kn, window, session=session, use_cache=use_cache)
    anns = stages.dedupe_by_notice(anns)
    print(f"[edu-bid] 수집·dedupe: {len(anns)}건")

    # S3 트리아지 (역량 키워드) — 비용 깔때기
    kw_index = stages.build_keyword_index(kn.capability_profile)
    candidates = [(a, m) for a in anns if (m := stages.triage(a, kw_index))]
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
    print(f"[edu-bid] 사업유형 무관 제외: {dropped_wt}건 / 남은 후보: {len(kept)}건")

    # S2 게이트 — 참가 불가(fail)는 평가 전에 제외해 LLM 비용 절감.
    # near_miss/pass 만 평가 대상으로 넘긴다(near_miss 는 결정 단계에서 미래타깃 처리).
    gated: list[GatedCandidate] = []
    dropped_gate = 0
    for ann, matched in kept:
        g = stages.gate(ann, kn.eligibility_ledger)
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
    return PrepareResult(
        gated=gated,
        collected=len(anns),
        triaged=len(candidates),
        dropped_work_type=dropped_wt,
        dropped_gate=dropped_gate,
    )


def run_track(
    track_name: str,
    work_types: list[str],
    gated: list[GatedCandidate],
    knowledge,
    model: str,
    batch_size: int,
    *,
    do_enrich: bool = True,
    session=None,
) -> list[Decision]:
    """트랙 하단부. 사업유형이 이 트랙에 속한 후보만 트랙 지식으로 평가·결정·정독한다."""
    kn = knowledge
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
