"""사내 플러그인 Marketplace 설치 URL 테스트입니다."""

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.plugin_marketplace import router


def marketplace_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_marketplace_설치_url을_비공개_git_저장소로_연결한다(monkeypatch):
    monkeypatch.setenv(
        "TMN_PLUGIN_MARKETPLACE_GIT_URL",
        "https://git.example.com/team-monolith/internal-plugins.git",
    )

    with TestClient(marketplace_app()) as client:
        response = client.get(
            "/plugins/tmn-operating.git/info/refs?service=git-upload-pack",
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://git.example.com/team-monolith/internal-plugins.git/"
        "info/refs?service=git-upload-pack"
    )


def test_git_upload_pack_post도_method를_유지하는_redirect를_쓴다(monkeypatch):
    monkeypatch.setenv(
        "TMN_PLUGIN_MARKETPLACE_GIT_URL",
        "https://git.example.com/team-monolith/internal-plugins.git",
    )

    with TestClient(marketplace_app()) as client:
        response = client.post(
            "/plugins/tmn-operating.git/git-upload-pack",
            content=b"git request",
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://git.example.com/team-monolith/internal-plugins.git/git-upload-pack"
    )
