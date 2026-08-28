"""사내 플러그인 Marketplace의 고정 설치 URL을 제공합니다."""

import os
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

DEFAULT_MARKETPLACE_GIT_URL = (
    "https://github.com/team-monolith-product/tmn-internal-plugins.git"
)


def marketplace_git_url() -> str:
    """비공개 Marketplace Git 원본 주소를 반환합니다."""
    url = os.environ.get(
        "TMN_PLUGIN_MARKETPLACE_GIT_URL", DEFAULT_MARKETPLACE_GIT_URL
    ).rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("TMN_PLUGIN_MARKETPLACE_GIT_URL은 HTTPS URL이어야 합니다.")
    return url


def redirected_git_url(request: Request, git_path: str = "") -> str:
    """Git smart HTTP 하위 경로와 query string을 원본에 그대로 전달합니다."""
    target = marketplace_git_url()
    if git_path:
        target = f"{target}/{git_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


@router.api_route(
    "/plugins/tmn-operating.git",
    methods=["GET", "POST"],
    include_in_schema=False,
)
@router.api_route(
    "/plugins/tmn-operating.git/{git_path:path}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def redirect_tmn_operating_marketplace(
    request: Request, git_path: str = ""
) -> RedirectResponse:
    """Codex·Claude의 Git 요청을 비공개 사내 Marketplace로 연결합니다."""
    return RedirectResponse(redirected_git_url(request, git_path), status_code=307)
