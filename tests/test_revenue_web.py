from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from decimal import Decimal
from starlette.testclient import TestClient

from app import revenue_web
from app.revenue_web import (
    DATA_MARKER,
    PKCE_COOKIE,
    REFRESH_COOKIE,
    SESSION_COOKIE,
    render_dashboard,
    router,
)

ADMIN = {"id": 7, "email": "lch@team-mono.com", "permissions": [], "tenants": []}
BASE = "https://wfa.codle.io"
JAR = {"domain": "wfa.codle.io", "path": "/revenue"}


def cookie(client, name):
    return client.cookies.get(name, **JAR)


def set_cookie(client, name, value):
    client.cookies.set(name, value, **JAR)


def state_of(client) -> str:
    return cookie(client, PKCE_COOKIE).split(".", 1)[0]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", BASE)
    monkeypatch.setenv("REVENUE_OAUTH_CLIENT_ID", "client-uid")
    monkeypatch.delenv("REVENUE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(
        revenue_web,
        "dashboard_data",
        lambda *args: {
            "rows": [{"customer": "도담고등학교", "amount": Decimal("1.25")}]
        },
    )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url=BASE) as tc:
        yield tc


def test_로그인_없으면_admin_rails_인가_화면으로_보낸다(client):
    response = client.get("/revenue/", follow_redirects=False)

    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert (
        location.netloc == "admin-rails.codle.io"
        and location.path == "/oauth/authorize"
    )
    query = parse_qs(location.query)
    assert query["client_id"] == ["client-uid"]
    assert query["redirect_uri"] == [f"{BASE}/revenue/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [state_of(client)]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "secure" in response.headers["set-cookie"].lower()


def test_슬래시_없는_경로는_슬래시로_보낸다(client):
    response = client.get("/revenue", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/revenue/"


def test_콜백은_코드를_토큰으로_바꿔_쿠키에_두고_대시보드로_보낸다(client):
    client.get("/revenue/", follow_redirects=False)
    state = state_of(client)
    exchange = AsyncMock(
        return_value={
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 1800,
        }
    )

    with patch("app.revenue_web.exchange_authorization_code", exchange):
        response = client.get(
            f"/revenue/callback?code=abc&state={state}", follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/revenue/"
    assert cookie(client, SESSION_COOKIE) == "at-1"
    assert cookie(client, REFRESH_COOKIE) == "rt-1"
    assert cookie(client, PKCE_COOKIE) is None
    args = exchange.await_args.args
    assert args[0] == "abc" and args[1] == f"{BASE}/revenue/callback"
    assert args[2] == "client-uid"


def test_state_가_다르면_거절한다(client):
    client.get("/revenue/", follow_redirects=False)

    response = client.get(
        "/revenue/callback?code=abc&state=forged", follow_redirects=False
    )

    assert response.status_code == 400


def test_낡은_코드면_다시_로그인으로_보낸다(client):
    client.get("/revenue/", follow_redirects=False)
    state = state_of(client)

    with patch(
        "app.revenue_web.exchange_authorization_code", AsyncMock(return_value=None)
    ):
        response = client.get(
            f"/revenue/callback?code=old&state={state}", follow_redirects=False
        )

    assert response.status_code == 302
    assert "/oauth/authorize" in response.headers["location"]


def test_유효한_쿠키면_DB_조회_화면을_돌려준다(client):
    set_cookie(client, SESSION_COOKIE, "at-1")

    with patch("app.revenue_web.get_me", AsyncMock(return_value=ADMIN)) as me:
        response = client.get("/revenue/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert DATA_MARKER not in response.text
    assert '"amount":"1.25"' in response.text
    assert "도담고등학교" in response.text
    assert me.await_count == 1


def test_만료된_토큰은_리프레시로_살린다(client):
    set_cookie(client, SESSION_COOKIE, "expired")
    set_cookie(client, REFRESH_COOKIE, "rt-1")
    me = AsyncMock(side_effect=lambda token: ADMIN if token == "at-2" else None)
    refresh = AsyncMock(
        return_value={
            "access_token": "at-2",
            "refresh_token": "rt-2",
            "expires_in": 1800,
        }
    )

    with patch("app.revenue_web.get_me", me), patch(
        "app.revenue_web.refresh_access_token", refresh
    ):
        response = client.get("/revenue/")

    assert response.status_code == 200
    assert cookie(client, SESSION_COOKIE) == "at-2"
    assert cookie(client, REFRESH_COOKIE) == "rt-2"
    assert refresh.await_args.args[0] == "rt-1"


def test_리프레시도_안_되면_로그인으로_보낸다(client):
    set_cookie(client, SESSION_COOKIE, "expired")
    set_cookie(client, REFRESH_COOKIE, "revoked")

    with patch("app.revenue_web.get_me", AsyncMock(return_value=None)), patch(
        "app.revenue_web.refresh_access_token", AsyncMock(return_value=None)
    ):
        response = client.get("/revenue/", follow_redirects=False)

    assert response.status_code == 302


def test_로그아웃은_쿠키를_지운다(client):
    set_cookie(client, SESSION_COOKIE, "at-1")

    response = client.get("/revenue/logout", follow_redirects=False)

    assert response.status_code == 303
    assert cookie(client, SESSION_COOKIE) is None


def test_정적_자산은_디렉터리_밖으로_못_나간다(client):
    assert client.get("/revenue/assets/logo-monolith.png").status_code == 200
    assert client.get("/revenue/assets/..%2Frevenue_web.py").status_code == 404
    assert client.get("/revenue/assets/없는파일.png").status_code == 404


def test_템플릿은_분류_이름과_색을_들고_있지_않다():
    html = revenue_web.TEMPLATE_PATH.read_text(encoding="utf-8")

    for name in (
        "코들 라이선스",
        "해커톤",
        "연수용역",
        "교육용역",
        "--s1:",
        "--s8:",
        "data-slot",
    ):
        assert name not in html, name
    assert "function bars(" in html


def test_렌더는_script_종료_태그를_무력화한다():
    data = {"rows": [{"note": "</script><script>alert(1)</script>"}]}

    html = render_dashboard(data)

    assert "</script><script>alert(1)" not in html
    assert "<\\/script><script>alert(1)" in html


def test_query_parameters_reach_database(client, monkeypatch):
    set_cookie(client, SESSION_COOKIE, "at-1")
    calls = []
    monkeypatch.setattr(
        revenue_web, "dashboard_data", lambda *args: calls.append(args) or {}
    )
    with patch("app.revenue_web.get_me", AsyncMock(return_value=ADMIN)):
        response = client.get(
            "/revenue/?year=2025&dimension=budget_sources&search=학교&page=2&status=planned"
        )
    assert response.status_code == 200
    assert calls == [(2025, "budget_sources", "학교", 2, "planned")]


def test_invalid_query_dimension_is_rejected(client):
    response = client.get("/revenue/?dimension=invalid")
    assert response.status_code == 422


def test_admin_access_is_checked_on_each_request(client):
    set_cookie(client, SESSION_COOKIE, "token")
    with patch("app.revenue_web.get_me", AsyncMock(side_effect=[ADMIN, None])) as me:
        assert client.get("/revenue/", follow_redirects=False).status_code == 200
        assert client.get("/revenue/", follow_redirects=False).status_code == 302
    assert me.await_count == 2
