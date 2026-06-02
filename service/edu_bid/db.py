"""
edu_bid DB 레이어 — enk Postgres 에 일일 분석 결과를 적재한다.

크롤러(잡)는 매 실행마다 Run 1건 + Decision N건(추천/검토/제외 전부 + 게이트 사유·축점수)을
기록한다. 이후 WA 어드민 프론트가 이 데이터를 조회·디버그·피드백에 쓴다.

연결은 DATABASE_URL 환경변수로 한다(클러스터 시크릿). 미설정이면 크롤러가 적재를
건너뛰므로(scripts/crawl_education_bids.py), enk 연결 전 단계에서도 일일 잡이 깨지지 않는다.
"""

import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


class EduBidRun(Base):
    """일일 실행 1건. 공유 상단부(prepare)의 깔때기 카운트를 함께 담는다."""

    __tablename__ = "edu_bid_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    window_bgn: Mapped[str] = mapped_column(String(12))  # YYYYMMDDHHMM
    window_end: Mapped[str] = mapped_column(String(12))
    run_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    collected: Mapped[int] = mapped_column(Integer, default=0)
    triaged: Mapped[int] = mapped_column(Integer, default=0)
    dropped_work_type: Mapped[int] = mapped_column(Integer, default=0)
    dropped_gate: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    decisions: Mapped[list["EduBidDecision"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EduBidDecision(Base):
    """평가된 공고 1건의 결정. 제외(낮은 점수)건도 디버그 위해 함께 저장한다."""

    __tablename__ = "edu_bid_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("edu_bid_runs.id", ondelete="CASCADE"), index=True
    )
    track: Mapped[str] = mapped_column(String(16), index=True)  # dev | content | edu
    label: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    enriched: Mapped[bool] = mapped_column(Boolean, default=False)

    # 공고 메타 (Announcement 스냅샷 — 원본 캐시와 무관히 그 시점 값을 보존)
    bid_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    bid_ord: Mapped[str] = mapped_column(String(16), default="")
    kind_label: Mapped[str] = mapped_column(String(32), default="")
    stage: Mapped[str] = mapped_column(String(16), default="")
    work_type: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    notice_inst: Mapped[str] = mapped_column(Text, default="")
    demand_inst: Mapped[str] = mapped_column(Text, default="")
    close_dt: Mapped[str] = mapped_column(String(32), default="")
    opinion_close_dt: Mapped[str] = mapped_column(String(32), default="")
    estimated_price: Mapped[str] = mapped_column(String(32), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    award_method: Mapped[str] = mapped_column(String(128), default="")

    # 평가 결과
    quant_barrier: Mapped[str] = mapped_column(String(16), default="")
    wired_risk: Mapped[str] = mapped_column(String(16), default="")
    axes: Mapped[dict] = mapped_column(JSON, default=dict)
    matched_assets: Mapped[list] = mapped_column(JSON, default=list)
    gate_status: Mapped[str] = mapped_column(String(16), default="")
    gate_reasons: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")

    run: Mapped["EduBidRun"] = relationship(back_populates="decisions")


_Session = None


def get_session_factory() -> sessionmaker:
    """DATABASE_URL 로 엔진·세션 팩토리를 lazy 생성(프로세스 1회)."""
    global _Session
    if _Session is None:
        url = os.environ["DATABASE_URL"]
        engine = create_engine(url, pool_pre_ping=True)
        _Session = sessionmaker(bind=engine)
    return _Session


@contextmanager
def session_scope():
    """커밋/롤백/클로즈를 묶는 세션 컨텍스트."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
