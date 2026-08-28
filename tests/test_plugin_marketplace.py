"""사내 플러그인 Marketplace 설치 URL 테스트입니다."""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.plugin_marketplace import router

MARKETPLACE_PATH = "/plugins/tmn-operating.git"
GITHUB_MARKETPLACE_URL = (
    "https://github.com/team-monolith-product/tmn-internal-plugins.git"
)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def test_info_refs_redirect는_사용자_query를_목적지에_전달하지_않는다(client):
    response = client.get(
        f"{MARKETPLACE_PATH}/info/refs"
        "?service=git-receive-pack&next=https://evil.example",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == (
        f"{GITHUB_MARKETPLACE_URL}/info/refs?service=git-upload-pack"
    )


def test_git_upload_pack_post는_method를_유지하는_redirect를_쓴다(client):
    response = client.post(
        f"{MARKETPLACE_PATH}/git-upload-pack",
        content=b"git request",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == (f"{GITHUB_MARKETPLACE_URL}/git-upload-pack")


def test_git_write와_임의_하위_경로는_노출하지_않는다(client):
    assert client.post(f"{MARKETPLACE_PATH}/git-receive-pack").status_code == 404
    assert client.get(f"{MARKETPLACE_PATH}/arbitrary").status_code == 404
