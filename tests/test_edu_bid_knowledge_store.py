"""edu_bid 지식 문서 저장소 테스트 — SQLite 로 버전·활성·트랙분리·DB우선 검증(Postgres 불필요)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from service.edu_bid import knowledge, knowledge_store, db
from service.edu_bid.db import Base, EduBidKnowledgeDocument


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_save_and_get_active(session):
    assert (
        knowledge_store.get_active_document("capability_profile", session=session)
        is None
    )
    v1 = knowledge_store.save_document(
        "capability_profile",
        "",
        {"assets": [{"id": "a"}]},
        author="seed",
        session=session,
    )
    assert v1 == 1
    doc = knowledge_store.get_active_document("capability_profile", session=session)
    assert doc == {"assets": [{"id": "a"}]}


def test_new_version_supersedes_and_track_isolated(session):
    knowledge_store.save_document(
        "scoring_policy",
        "dev",
        {"thresholds": {"recommend": 70}},
        author="seed",
        session=session,
    )
    v2 = knowledge_store.save_document(
        "scoring_policy",
        "dev",
        {"thresholds": {"recommend": 65}},
        author="lch",
        note="문턱 하향",
        session=session,
    )
    assert v2 == 2
    active = knowledge_store.get_active_document(
        "scoring_policy", "dev", session=session
    )
    assert active["thresholds"]["recommend"] == 65
    # (section, track) 당 활성은 한 건
    n_active = (
        session.query(EduBidKnowledgeDocument)
        .filter_by(section="scoring_policy", track="dev", active=True)
        .count()
    )
    assert n_active == 1
    # 트랙 분리 — content 는 영향 없음
    assert (
        knowledge_store.get_active_document(
            "scoring_policy", "content", session=session
        )
        is None
    )


def test_idempotent_same_payload(session):
    knowledge_store.save_document(
        "eligibility_ledger", "", {"x": 1}, author="seed", session=session
    )
    v = knowledge_store.save_document(
        "eligibility_ledger", "", {"x": 1}, author="seed", session=session
    )
    assert v == 1  # 동일 내용 → 새 버전 생략


def test_load_knowledge_prefers_db_over_yaml(monkeypatch, tmp_path):
    """DATABASE_URL 있으면 load_knowledge 가 DB 활성 문서를 YAML 보다 우선한다."""
    url = f"sqlite:///{tmp_path / 'k.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(db, "_Session", None)  # 팩토리 리셋(테스트 후 자동 복원)
    Base.metadata.create_all(create_engine(url))

    # YAML 기본 임계(recommend 70)와 다른 값을 DB 에 활성으로
    knowledge_store.save_document(
        "scoring_policy",
        "dev",
        {
            "strategy": {"primary": {"desc": "DB 전략"}, "secondary": {"desc": "-"}},
            "weights": {"reuse": 1.0},
            "thresholds": {"recommend": 99, "review": 50},
        },
        author="t",
    )

    kn = knowledge.load_knowledge("dev")
    assert kn.thresholds["recommend"] == 99  # YAML(70) 아님 → DB 우선
    assert kn.scoring_policy["strategy"]["primary"]["desc"] == "DB 전략"
    # DB 에 없는 공유 문서는 YAML 폴백 — 자산이 그대로 로드됨
    assert len(kn.shared.capability_profile.get("assets", [])) > 0
