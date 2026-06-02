"""
지식 문서 저장소 — 자산·정량규격·전략을 버전드 JSON 문서로 DB(SoT)에 읽고 쓴다.

- get_active_document: 활성 버전 payload(dict) 조회. 없으면 None.
- save_document: 새 버전 저장(직전 활성은 비활성화). 어드민 편집·시드 공용.

순수 DB 접근이라 테스트는 세션을 주입해 SQLite 로 검증한다(Postgres 불필요).
"""

import datetime

from sqlalchemy import select

from .db import EduBidKnowledgeDocument, session_scope


def _norm_track(track: str | None) -> str:
    return track or ""


def _json_safe(obj):
    """JSON 컬럼 저장용 정규화 — YAML 이 date 로 파싱한 값(updated/expires 등)을 ISO 문자열로.

    파이프라인 로직은 이 날짜 값을 쓰지 않으므로 문자열화해도 무해하다(저장 일관성 확보).
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return obj


def _query_active(session, section: str, track: str):
    return session.execute(
        select(EduBidKnowledgeDocument).where(
            EduBidKnowledgeDocument.section == section,
            EduBidKnowledgeDocument.track == track,
            EduBidKnowledgeDocument.active.is_(True),
        )
    ).scalar_one_or_none()


def get_active_document(
    section: str, track: str | None = None, *, session=None
) -> dict | None:
    """활성 버전의 payload 를 반환. 없으면 None (→ 호출측에서 YAML 폴백)."""
    track = _norm_track(track)
    if session is not None:
        row = _query_active(session, section, track)
        return row.payload if row else None
    with session_scope() as s:
        row = _query_active(s, section, track)
        return row.payload if row else None


def save_document(
    section: str,
    track: str | None,
    payload: dict,
    *,
    author: str,
    note: str = "",
    session=None,
) -> int:
    """새 버전을 활성으로 저장하고 직전 활성을 비활성화. 반환: 새 버전 번호.

    직전 활성과 payload 가 동일하면 새 버전을 만들지 않는다(시드 멱등성).
    """
    track = _norm_track(track)
    payload = _json_safe(payload)

    def _save(s) -> int:
        current = _query_active(s, section, track)
        if current is not None and current.payload == payload:
            return current.version  # 변경 없음 — 새 버전 생략
        version = (current.version + 1) if current else 1
        if current is not None:
            current.active = False
        s.add(
            EduBidKnowledgeDocument(
                section=section,
                track=track,
                version=version,
                active=True,
                payload=payload,
                author=author,
                note=note,
            )
        )
        return version

    if session is not None:
        return _save(session)
    with session_scope() as s:
        return _save(s)


def _doc_meta(row: EduBidKnowledgeDocument) -> dict:
    return {
        "section": row.section,
        "track": row.track,
        "version": row.version,
        "active": row.active,
        "author": row.author,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_active(*, session=None) -> list[dict]:
    """활성 문서들의 메타(payload 제외)를 section/track 순으로 반환."""

    def _list(s) -> list[dict]:
        rows = (
            s.execute(
                select(EduBidKnowledgeDocument)
                .where(EduBidKnowledgeDocument.active.is_(True))
                .order_by(
                    EduBidKnowledgeDocument.section, EduBidKnowledgeDocument.track
                )
            )
            .scalars()
            .all()
        )
        return [_doc_meta(r) for r in rows]

    if session is not None:
        return _list(session)
    with session_scope() as s:
        return _list(s)


def list_versions(
    section: str, track: str | None = None, *, session=None
) -> list[dict]:
    """한 문서의 전체 버전 메타를 최신순으로 반환(이력·롤백 UI 용)."""
    track = _norm_track(track)

    def _list(s) -> list[dict]:
        rows = (
            s.execute(
                select(EduBidKnowledgeDocument)
                .where(
                    EduBidKnowledgeDocument.section == section,
                    EduBidKnowledgeDocument.track == track,
                )
                .order_by(EduBidKnowledgeDocument.version.desc())
            )
            .scalars()
            .all()
        )
        return [_doc_meta(r) for r in rows]

    if session is not None:
        return _list(session)
    with session_scope() as s:
        return _list(s)
