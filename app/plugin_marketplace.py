"""사내 플러그인 Marketplace의 읽기 전용 Git 설치 URL입니다."""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(
    prefix="/plugins/tmn-operating.git",
    include_in_schema=False,
)

MARKETPLACE_GIT_URL = (
    "https://github.com/team-monolith-product/tmn-internal-plugins.git"
)


@router.get("/info/refs")
async def redirect_info_refs() -> RedirectResponse:
    """Git 저장소 발견 요청을 고정된 비공개 원본으로 보냅니다."""
    return RedirectResponse(
        f"{MARKETPLACE_GIT_URL}/info/refs?service=git-upload-pack",
        status_code=307,
    )


@router.post("/git-upload-pack")
async def redirect_upload_pack() -> RedirectResponse:
    """읽기 전용 Git 데이터 요청을 고정된 비공개 원본으로 보냅니다."""
    return RedirectResponse(
        f"{MARKETPLACE_GIT_URL}/git-upload-pack",
        status_code=307,
    )
