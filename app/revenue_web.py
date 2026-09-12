"""매출 대시보드. admin-rails로 인증하고 DB 조회 결과를 표시한다."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from api.admin_rails import exchange_authorization_code, get_me, refresh_access_token
from service.revenue.query import dashboard_data

router = APIRouter(prefix="/revenue")

TEMPLATE_PATH = Path(__file__).with_name("revenue_dashboard.html")
ASSETS_DIR = Path(__file__).with_name("revenue_assets")
DATA_MARKER = "/*__DATA__*/null"

SESSION_COOKIE = "revenue_session"
REFRESH_COOKIE = "revenue_refresh"
PKCE_COOKIE = "revenue_pkce"
# 리프레시 토큰은 admin-rails 기본값으로 만료가 없다. 브라우저 쪽에서 30일이면 끊는다.
REFRESH_MAX_AGE = 30 * 24 * 3600
PKCE_MAX_AGE = 10 * 60

# 같은 토큰으로 요청이 이어질 때마다 admin-rails 를 부르지 않는다. 폐기가 반영되는
# 지연은 이 안이다.
ME_CACHE_SECONDS = 60
_me_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _base_url() -> str:
    return os.environ["MCP_RESOURCE_URL"].rstrip("/")


def _client_id() -> str:
    return os.environ["REVENUE_OAUTH_CLIENT_ID"]


def _client_secret() -> str | None:
    return os.environ.get("REVENUE_OAUTH_CLIENT_SECRET") or None


def _secure_cookies() -> bool:
    return _base_url().startswith("https://")


def _redirect_uri() -> str:
    return f"{_base_url()}/revenue/callback"


def _set_cookie(
    response: Response, name: str, value: str, max_age: int, path: str = "/revenue"
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=path,
        httponly=True,
        secure=_secure_cookies(),
        samesite="lax",
    )


async def _admin_for(token: str) -> dict[str, Any] | None:
    """토큰이 가리키는 어드민. 60초 캐시."""
    key = hashlib.sha256(token.encode()).hexdigest()
    cached = _me_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < ME_CACHE_SECONDS:
        return cached[1]
    admin = await get_me(token)
    if admin is not None:
        _me_cache[key] = (now, admin)
    return admin


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _login_redirect(request: Request) -> RedirectResponse:
    """admin-rails 인가 화면으로 보낸다. state·verifier 는 짧은 쿠키에 둔다."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize = f"{os.environ['ADMIN_RAILS_BASE_URL'].rstrip('/')}/oauth/authorize?{urlencode(params)}"
    response = RedirectResponse(authorize, status_code=302)
    # 둘 다 urlsafe 토큰이라 점으로 이으면 된다. JSON 은 따옴표 때문에 쿠키에서 깨진다.
    _set_cookie(response, PKCE_COOKIE, f"{state}.{verifier}", PKCE_MAX_AGE)
    return response


def render_dashboard(data: dict[str, Any]) -> str:
    """집계 결과와 거래 한 페이지를 HTML로 전달한다."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), default=_json_value
    )
    # </script> 가 데이터 안에 있으면 문서가 거기서 끊긴다.
    return template.replace(DATA_MARKER, payload.replace("</", "<\\/"))


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"JSON으로 변환할 수 없음: {type(value)}")


@router.get("")
async def revenue_root() -> RedirectResponse:
    """상대 경로(assets/…)가 /revenue/ 기준으로 풀리도록 슬래시를 붙인다."""
    return RedirectResponse("/revenue/", status_code=307)


@router.get("/")
async def revenue_dashboard(
    request: Request,
    year: int | None = None,
    dimension: Literal[
        "category", "subcategory", "school_level", "edu_office", "budget_sources"
    ] = "category",
    search: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    status: Literal["issued", "planned"] = "issued",
) -> Response:
    """
    매출 대시보드. 로그인이 없으면 admin-rails 로 보내고, 만료면 리프레시를 먼저 시도한다.
    """
    token = request.cookies.get(SESSION_COOKIE)
    admin = await _admin_for(token) if token else None

    refreshed: dict[str, Any] | None = None
    refresh = request.cookies.get(REFRESH_COOKIE)
    if admin is None and refresh:
        refreshed = await refresh_access_token(refresh, _client_id(), _client_secret())
        if refreshed:
            admin = await _admin_for(refreshed["access_token"])

    if admin is None:
        return _login_redirect(request)

    data = await asyncio.to_thread(
        dashboard_data, year, dimension, search, page, status
    )
    print(f"[revenue] dashboard by {admin['email']}")
    response = HTMLResponse(render_dashboard(data))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if refreshed:
        _set_cookie(
            response, SESSION_COOKIE, refreshed["access_token"], refreshed["expires_in"]
        )
        if refreshed.get("refresh_token"):
            _set_cookie(
                response, REFRESH_COOKIE, refreshed["refresh_token"], REFRESH_MAX_AGE
            )
    return response


@router.get("/callback")
async def revenue_callback(request: Request, code: str, state: str) -> Response:
    """admin-rails 가 돌려보낸 인가 코드를 토큰으로 바꾸고 대시보드로 보낸다."""
    pkce_raw = request.cookies.get(PKCE_COOKIE)
    if not pkce_raw:
        # 10분이 지났거나 다른 브라우저다. 처음부터 다시.
        return _login_redirect(request)
    expected_state, verifier = pkce_raw.split(".", 1)
    if not secrets.compare_digest(expected_state, state):
        return Response("state 가 맞지 않습니다. 다시 로그인하십시오.", status_code=400)

    tokens = await exchange_authorization_code(
        code, _redirect_uri(), _client_id(), verifier, _client_secret()
    )
    if tokens is None:
        # 코드는 10분이면 낡고 한 번만 쓰인다. 뒤로 가기로 돌아온 경우다.
        return _login_redirect(request)
    response = RedirectResponse("/revenue/", status_code=303)
    _set_cookie(response, SESSION_COOKIE, tokens["access_token"], tokens["expires_in"])
    if tokens.get("refresh_token"):
        _set_cookie(response, REFRESH_COOKIE, tokens["refresh_token"], REFRESH_MAX_AGE)
    response.delete_cookie(PKCE_COOKIE, path="/revenue")
    return response


@router.get("/logout")
async def revenue_logout() -> RedirectResponse:
    """쿠키만 지운다. admin-rails 세션은 그대로다."""
    response = RedirectResponse("/revenue/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/revenue")
    response.delete_cookie(REFRESH_COOKIE, path="/revenue")
    return response


@router.get("/assets/{name}")
async def revenue_asset(name: str) -> Response:
    """로고 같은 정적 파일. 요청한 이름으로 경로를 만들지 않고 디렉터리 목록에서 고른다."""
    files = {entry.name: entry for entry in ASSETS_DIR.iterdir() if entry.is_file()}
    if name not in files:
        return Response("Not Found", status_code=404)
    return FileResponse(files[name], headers={"Cache-Control": "public, max-age=86400"})
