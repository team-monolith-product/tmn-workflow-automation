import asyncio
import base64
import hashlib
import json
import os
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from api.admin_rails import exchange_authorization_code, get_me, refresh_access_token
from service.revenue.query import dashboard_data

router = APIRouter(prefix="/revenue")

TEMPLATE_PATH = Path(__file__).with_name("dashboard.html")
DATA_MARKER = "__REVENUE_DATA__"

SESSION_COOKIE = "revenue_session"
REFRESH_COOKIE = "revenue_refresh"
PKCE_COOKIE = "revenue_pkce"
REFRESH_MAX_AGE = 30 * 24 * 3600
PKCE_MAX_AGE = 10 * 60


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


def _set_tokens(response: Response, tokens: dict[str, Any]) -> None:
    _set_cookie(response, SESSION_COOKIE, tokens["access_token"], tokens["expires_in"])
    if tokens.get("refresh_token"):
        _set_cookie(response, REFRESH_COOKIE, tokens["refresh_token"], REFRESH_MAX_AGE)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _login_redirect() -> RedirectResponse:
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
    _set_cookie(response, PKCE_COOKIE, f"{state}.{verifier}", PKCE_MAX_AGE)
    return response


def render_dashboard(data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(
        jsonable_encoder(data, custom_encoder={Decimal: str}),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return template.replace(DATA_MARKER, payload.replace("</", "<\\/"))


@router.get("")
async def revenue_root() -> RedirectResponse:
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
    bucket: str | None = Query(default=None, max_length=200),
    subcategory: str | None = Query(default=None, max_length=200),
    format: Literal["html", "json"] = "html",
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    admin = await get_me(token) if token else None

    refreshed: dict[str, Any] | None = None
    refresh = request.cookies.get(REFRESH_COOKIE)
    if admin is None and refresh:
        refreshed = await refresh_access_token(refresh, _client_id(), _client_secret())
        if refreshed:
            admin = await get_me(refreshed["access_token"])

    if admin is None:
        return _login_redirect()

    data = await asyncio.to_thread(
        dashboard_data, year, dimension, search, page, status, bucket, subcategory
    )
    print(f"[revenue] dashboard by {admin['email']}")
    response = (
        JSONResponse(jsonable_encoder(data, custom_encoder={Decimal: str}))
        if format == "json"
        else HTMLResponse(render_dashboard(data))
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if refreshed:
        _set_tokens(response, refreshed)
    return response


@router.get("/callback")
async def revenue_callback(request: Request, code: str, state: str) -> Response:
    pkce_raw = request.cookies.get(PKCE_COOKIE)
    if not pkce_raw:
        return _login_redirect()
    expected_state, verifier = pkce_raw.split(".", 1)
    if not secrets.compare_digest(expected_state, state):
        return Response("state 가 맞지 않습니다. 다시 로그인하십시오.", status_code=400)

    tokens = await exchange_authorization_code(
        code, _redirect_uri(), _client_id(), verifier, _client_secret()
    )
    if tokens is None:
        return _login_redirect()
    response = RedirectResponse("/revenue/", status_code=303)
    _set_tokens(response, tokens)
    response.delete_cookie(PKCE_COOKIE, path="/revenue")
    return response


@router.get("/logout")
async def revenue_logout() -> RedirectResponse:
    response = RedirectResponse("/revenue/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/revenue")
    response.delete_cookie(REFRESH_COOKIE, path="/revenue")
    return response


@router.get("/assets/logo-monolith.png")
async def revenue_logo() -> Response:
    return FileResponse(
        TEMPLATE_PATH.with_name("logo-monolith.png"),
        headers={"Cache-Control": "public, max-age=86400"},
    )
