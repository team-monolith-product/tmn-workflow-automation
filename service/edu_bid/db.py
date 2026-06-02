"""
edu_bid DB 레이어 — 전략 지식(자산·정량규격·전략)을 버전드 JSON 문서로 저장한다.

이 DB 가 지식의 원본(SoT)이다. 어드민(후속)에서 문서를 편집하면 새 버전이 쌓이고,
파이프라인은 활성 버전을 읽는다. YAML(knowledge/edu_bid/*.yaml)은 최초 1회 시드용이며,
DB 에 문서가 없으면 YAML 로 폴백한다(부트스트랩·로컬 개발).

연결은 DATABASE_URL 환경변수로 한다(클러스터 시크릿). 미설정이면 YAML 만 쓴다.
"""

import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class EduBidKnowledgeDocument(Base):
    """지식 문서 1버전. (section, track) 당 active=True 는 한 건만 유지(코드에서 보장)."""

    __tablename__ = "edu_bid_knowledge_documents"
    __table_args__ = (
        UniqueConstraint("section", "track", "version", name="uq_knowledge_doc_ver"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # capability_profile | eligibility_ledger | scoring_policy
    section: Mapped[str] = mapped_column(String(32), index=True)
    # 공유 문서는 "" , 트랙별(scoring_policy)은 트랙 key(dev/content/edu)
    track: Mapped[str] = mapped_column(String(16), default="", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON)  # YAML 과 동일 구조의 문서 본문
    author: Mapped[str] = mapped_column(
        String(64), default=""
    )  # 편집자(어드민 사용자/seed)
    note: Mapped[str] = mapped_column(Text, default="")  # 변경 메모
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
