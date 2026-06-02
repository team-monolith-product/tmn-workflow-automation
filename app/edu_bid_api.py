"""
교육 입찰 전략 지식 관리 API (WA 어드민 프론트용)

자산·정량규격·전략 문서를 조회/이력/편집한다. DB(SoT)에 버전드 JSON 으로 저장되며,
편집은 새 버전을 활성화한다. main.py(FastAPI)에 include_router 로 붙는다.

인증: admin-rails(Doorkeeper) 액세스 토큰을 Bearer 로 받아 admin-rails /api/v1/me 로
검증한다(어드민 프론트가 admin-rails 로 로그인 → 토큰을 이 API 에 전달). AUTH_SERVER 환경변수
= admin-rails 베이스 URL.
"""

import os

import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel

from service.edu_bid import knowledge_store

router = APIRouter(prefix="/api/edu-bid", tags=["edu-bid"])

# DB(SoT)로 관리하는 지식 섹션. scoring_policy 만 트랙별, 나머지는 공유(track="").
_SHARED_SECTIONS = {"capability_profile", "eligibility_ledger"}
_TRACK_SECTIONS = {"scoring_policy"}
_SECTIONS = _SHARED_SECTIONS | _TRACK_SECTIONS


class SaveBody(BaseModel):
    payload: dict
    note: str = ""


def verify_admin(authorization: str | None = Header(None)) -> dict:
    """admin-rails 토큰 검증. 통과 시 사용자(dict) 반환, 실패 시 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer 토큰이 필요합니다"
        )
    token = authorization.split(" ", 1)[1]
    auth_server = os.environ.get("AUTH_SERVER")
    if not auth_server:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AUTH_SERVER(admin-rails) 환경변수가 없습니다",
        )
    resp = requests.get(
        f"{auth_server}/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰"
        )
    return resp.json()


def _validate(section: str, track: str) -> str:
    """섹션·트랙 정합성 검증 후 정규화된 track 반환."""
    if section not in _SECTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"알 수 없는 섹션 '{section}'. 가능: {sorted(_SECTIONS)}",
        )
    if section in _TRACK_SECTIONS and not track:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"섹션 '{section}' 은 track 이 필요합니다",
        )
    return "" if section in _SHARED_SECTIONS else track


@router.get("/knowledge")
def list_knowledge(_: dict = Depends(verify_admin)) -> list[dict]:
    """활성 지식 문서 목록(메타). 어드민 목록 화면."""
    return knowledge_store.list_active()


@router.get("/knowledge/{section}")
def get_knowledge(
    section: str, track: str = Query(""), _: dict = Depends(verify_admin)
) -> dict:
    """활성 문서 본문 조회."""
    track = _validate(section, track)
    doc = knowledge_store.get_active_document(section, track)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="활성 문서가 없습니다"
        )
    return {"section": section, "track": track, "payload": doc}


@router.get("/knowledge/{section}/versions")
def get_knowledge_versions(
    section: str, track: str = Query(""), _: dict = Depends(verify_admin)
) -> list[dict]:
    """문서 버전 이력(최신순). 롤백·감사 화면."""
    track = _validate(section, track)
    return knowledge_store.list_versions(section, track)


@router.put("/knowledge/{section}")
def put_knowledge(
    section: str,
    body: SaveBody,
    track: str = Query(""),
    user: dict = Depends(verify_admin),
) -> dict:
    """문서 편집 — 새 버전을 활성으로 저장. 작성자는 토큰 사용자."""
    track = _validate(section, track)
    author = user.get("email") or user.get("name") or "admin"
    version = knowledge_store.save_document(
        section, track, body.payload, author=author, note=body.note
    )
    return {"section": section, "track": track, "version": version}
