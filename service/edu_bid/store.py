"""
적재 — 파이프라인 산출물(PrepareResult + 트랙별 Decision)을 DB 행으로 변환·저장한다.

순수 매핑이라 SQLite 세션으로도 단위 테스트가 된다(네트워크·Postgres 불필요).
"""

from .db import EduBidRun, EduBidDecision
from .schemas import Decision


def _to_row(track_key: str, d: Decision) -> EduBidDecision:
    """Decision(+Announcement) → EduBidDecision 행."""
    a = d.announcement
    return EduBidDecision(
        track=track_key,
        label=d.label,
        score=d.score,
        enriched=d.enriched,
        bid_no=a.bid_no,
        bid_ord=a.bid_ord,
        kind_label=a.kind_label,
        stage=a.stage,
        work_type=a.work_type,
        title=a.title,
        notice_inst=a.notice_inst,
        demand_inst=a.demand_inst,
        close_dt=a.close_dt,
        opinion_close_dt=a.opinion_close_dt,
        estimated_price=a.estimated_price,
        url=a.url,
        award_method=a.award_method,
        quant_barrier=d.quant_barrier,
        wired_risk=d.wired_risk,
        axes=d.axes,
        matched_assets=d.matched_assets,
        gate_status=d.gate.status,
        gate_reasons=d.gate.reasons,
        rationale=d.rationale,
    )


def persist_run(
    session,
    window: tuple[str, str],
    prep,
    per_track: dict[str, list[Decision]],
) -> int:
    """Run 1건 + 트랙별 Decision 들을 저장하고 run_id 를 반환한다.

    prep 는 pipeline.prepare 의 PrepareResult(깔때기 카운트). per_track 은 {track_key: [Decision]}.
    """
    bgn = window[0]
    run = EduBidRun(
        window_bgn=bgn,
        window_end=window[1],
        run_date=f"{bgn[:4]}-{bgn[4:6]}-{bgn[6:8]}",
        collected=prep.collected,
        triaged=prep.triaged,
        dropped_work_type=prep.dropped_work_type,
        dropped_gate=prep.dropped_gate,
    )
    session.add(run)
    for track_key, decisions in per_track.items():
        for d in decisions:
            row = _to_row(track_key, d)
            row.run = run
            session.add(row)
    session.flush()  # run.id 확정
    return run.id
