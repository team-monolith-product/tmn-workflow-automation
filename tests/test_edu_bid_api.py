"""교육 입찰 지식 관리 API 테스트 — TestClient + SQLite, 인증은 오버라이드.

verify_admin(외부 admin-rails 호출)은 의존성 오버라이드로 대체하고, DB 는 임시 SQLite 로
띄워 라우터의 조회/편집/이력/검증 동작을 검증한다.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.edu_bid_api import router, verify_admin
from service.edu_bid import db
from service.edu_bid.db import Base


@pytest.fixture
def client(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'admin.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(db, "_Session", None)  # 팩토리 리셋(테스트 후 복원)
    Base.metadata.create_all(create_engine(url))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_admin] = lambda: {"email": "lch@team-mono.com"}
    return TestClient(app)


def test_list_empty(client):
    r = client.get("/api/edu-bid/knowledge")
    assert r.status_code == 200 and r.json() == []


def test_put_then_get_and_versions(client):
    r = client.put(
        "/api/edu-bid/knowledge/scoring_policy?track=dev",
        json={"payload": {"thresholds": {"recommend": 80}}, "note": "상향"},
    )
    assert r.status_code == 200 and r.json()["version"] == 1

    r = client.get("/api/edu-bid/knowledge/scoring_policy?track=dev")
    assert r.json()["payload"]["thresholds"]["recommend"] == 80

    client.put(
        "/api/edu-bid/knowledge/scoring_policy?track=dev",
        json={"payload": {"thresholds": {"recommend": 75}}},
    )
    versions = client.get(
        "/api/edu-bid/knowledge/scoring_policy/versions?track=dev"
    ).json()
    assert [v["version"] for v in versions] == [2, 1]  # 최신순
    assert versions[0]["author"] == "lch@team-mono.com"  # 토큰 사용자 기록

    # 목록에 활성 1건(최신 버전)
    listed = client.get("/api/edu-bid/knowledge").json()
    assert len(listed) == 1 and listed[0]["version"] == 2


def test_section_validation(client):
    assert client.get("/api/edu-bid/knowledge/unknown").status_code == 400
    # scoring_policy 는 track 필수
    assert (
        client.put(
            "/api/edu-bid/knowledge/scoring_policy", json={"payload": {}}
        ).status_code
        == 400
    )


def test_get_missing_returns_404(client):
    assert client.get("/api/edu-bid/knowledge/capability_profile").status_code == 404


def test_auth_required_without_override():
    # 오버라이드 없이 토큰 미제공 → 401 (엔드포인트 본문 진입 전)
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get("/api/edu-bid/knowledge").status_code == 401
