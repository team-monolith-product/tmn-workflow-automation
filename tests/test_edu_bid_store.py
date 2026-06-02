"""edu_bid 적재(store) 테스트 — SQLite 인메모리로 매핑·JSON 라운드트립 검증(Postgres 불필요)."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from service.edu_bid import store
from service.edu_bid.db import Base, EduBidDecision, EduBidRun
from service.edu_bid.pipeline import PrepareResult
from service.edu_bid.schemas import Announcement, Decision, GateResult


def _session():
    # 인메모리 SQLite — StaticPool 로 단일 커넥션 유지(create_all 과 세션이 같은 DB)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _decision(**over) -> Decision:
    a = Announcement(
        kind_label="용역",
        bid_no="R26BK1",
        bid_ord="000",
        title="LMS 고도화",
        notice_inst="",
        demand_inst="A대학교",
        close_dt="2026-06-09",
        estimated_price="90000000",
        url="https://g2b/x",
        award_method="협상에의한계약",
        work_type="개발",
    )
    return Decision(
        announcement=a,
        gate=GateResult("pass", ["업종제한 있음"]),
        axes={"reuse": 70, "winnability": 65, "value": 60, "performance_building": 75},
        quant_barrier="low",
        matched_assets=["gov_cloud_infra", "course_authoring"],
        score=72.3,
        label="입찰추천",
        rationale="재사용 높음",
        wired_risk="low",
        enriched=True,
    )


def test_persist_run_writes_run_and_decisions():
    s = _session()
    prep = PrepareResult(
        gated=[], collected=1194, triaged=195, dropped_work_type=49, dropped_gate=8
    )
    per_track = {"dev": [_decision()], "content": [], "edu": []}

    run_id = store.persist_run(s, ("202606010931", "202606020931"), prep, per_track)
    s.commit()

    run = s.get(EduBidRun, run_id)
    assert run.run_date == "2026-06-01"  # window_bgn 에서 파생
    assert run.collected == 1194 and run.dropped_gate == 8

    rows = s.query(EduBidDecision).filter_by(run_id=run_id).all()
    assert len(rows) == 1  # 빈 트랙은 행 없음
    d = rows[0]
    assert d.track == "dev" and d.label == "입찰추천" and d.score == 72.3
    assert d.bid_no == "R26BK1" and d.work_type == "개발"
    assert d.axes["reuse"] == 70  # JSON 라운드트립
    assert d.matched_assets == ["gov_cloud_infra", "course_authoring"]
    assert d.gate_status == "pass" and d.gate_reasons == ["업종제한 있음"]
    assert d.enriched is True


def test_persist_run_cascade_delete():
    s = _session()
    prep = PrepareResult(gated=[])
    run_id = store.persist_run(
        s, ("202606010931", "202606020931"), prep, {"dev": [_decision()]}
    )
    s.commit()

    run = s.get(EduBidRun, run_id)
    s.delete(run)
    s.commit()
    # ORM cascade(all, delete-orphan) 로 자식 Decision 도 삭제
    assert s.query(EduBidDecision).count() == 0
