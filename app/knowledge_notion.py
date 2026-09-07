"""
노션 웹훅을 받아 지식베이스에 반영하는 엔드포인트입니다.

슬랙의 Socket Mode에 대응하는 자리입니다. 슬랙은 소켓으로 이벤트를 받고
노션은 HTTP로 받는다는 차이뿐이고, 둘 다 "바뀐 것 하나를 곧바로 반영"을
맡습니다. 노션 색인은 이 자리로만 바뀝니다.

구독은 노션 연결 설정 화면에서 만듭니다. 처음 한 번 verification_token만 담긴
POST가 오는데, 그 값을 화면에 되돌려줘야 구독이 살아납니다. 받을 방법이 로그밖에
없어 그때만 찍습니다. 이후 모든 요청의 X-Notion-Signature가 이 토큰으로 서명되므로
NOTION_WEBHOOK_VERIFICATION_TOKEN에 넣어두어야 합니다.

응답을 먼저 돌려주고 적재는 그 뒤에 합니다. 페이지와 블록을 다시 받는 데 몇 초가
걸리는데, 그동안 응답을 붙들면 노션이 실패로 보고 재시도합니다.
"""

import os
from typing import Any

from cachetools import TTLCache
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from app.common import notion, notion_page_to_markdown
from app.knowledge import DISTILL_DELAY_SECONDS
from service.db import connect
from service.knowledge.ingest import upsert_item
from service.knowledge.notion import (
    MIN_BODY_CHARS,
    build_fetch_node,
    build_page_row,
    fetch_user_emails,
    is_database_row,
    node_title,
)
from service.knowledge.notion_events import (
    DELETE_EVENTS,
    INGEST_EVENTS,
    delete_page,
    resolve_root,
    verify_signature,
)
from service.knowledge.register import upsert_source

router = APIRouter()

# 작성자 매핑은 이벤트마다 다시 받을 만한 것이 아니다. 사람이 100명 남짓이고
# 새 계정이 한 시간 늦게 잡혀도 author가 bot:<id>로 한 번 적힐 뿐이다.
_user_emails: TTLCache = TTLCache(maxsize=1, ttl=3600)


def _notion_user_emails() -> dict[str, str]:
    """노션 사용자 ID → 이메일 매핑을 캐시해 돌려줍니다.

    Returns:
        dict[str, str]: 이메일이 있는 사람 계정만 담은 매핑
    """
    if "emails" not in _user_emails:
        _user_emails["emails"] = fetch_user_emails(notion)
    return _user_emails["emails"]


def ingest_page(page_id: str) -> str:
    """페이지 하나를 다시 받아 적재합니다.

    Args:
        page_id: 노션 페이지 ID

    Returns:
        str: 처리 결과를 적은 한 줄
    """
    page = notion.pages.retrieve(page_id)

    # 이벤트는 순서가 보장되지 않는다. 지운 뒤의 갱신 이벤트가 늦게 도착하면
    # 지운 페이지가 되살아난다.
    if page.get("in_trash") or page.get("archived"):
        with connect(None) as conn:
            removed = delete_page(conn, page_id)
            conn.commit()
        return f"휴지통 {page_id} 삭제 {removed}"

    cache: dict[str, dict[str, Any] | None] = {}
    root = resolve_root(page, build_fetch_node(notion, cache), cache)
    if root is None:
        # 사슬이 중간에 끊기면 최상위가 블록이나 지워진 데이터베이스의 ID가
        # 된다. 그것으로 data_source를 만들면 이름도 실체도 없는 출처가
        # 검색 결과에 찍힌다.
        return f"최상위를 못 찾음 {page_id}"

    markdown = notion_page_to_markdown(page_id) or ""
    if is_database_row(page) and len(markdown) < MIN_BODY_CHARS:
        # 표의 행이다. 구매 내역이나 학교 목록 같은 것이 검색에 섞이지 않도록
        # 본문 길이로 가른다.
        with connect(None) as conn:
            removed = delete_page(conn, page_id)
            conn.commit()
        return f"본문 {len(markdown)}자라 제외 {page_id}, 삭제 {removed}"

    with connect(None) as conn:
        data_source_id = upsert_source(
            conn, "notion", root["id"], node_title(root), {"kind": "root"}
        )
        upsert_item(
            conn,
            build_page_row(
                data_source_id=data_source_id,
                page=page,
                markdown=markdown,
                distill_delay_seconds=DISTILL_DELAY_SECONDS,
                user_emails=_notion_user_emails(),
            ),
        )
        moved = delete_page(conn, page_id, keep=data_source_id)
        conn.commit()

    return f"적재 {page_id} → {node_title(root)}" + (
        f", 옮겨져 {moved} 삭제" if moved else ""
    )


def process_event(event: dict[str, Any]) -> None:
    """이벤트 하나를 처리합니다.

    Args:
        event: 노션 웹훅 payload
    """
    entity = event.get("entity") or {}
    if entity.get("type") != "page":
        return

    event_type = event.get("type", "")
    if event_type in DELETE_EVENTS:
        with connect(None) as conn:
            removed = delete_page(conn, entity["id"])
            conn.commit()
        print(f"노션 웹훅 {event_type} 삭제 {removed} {entity['id']}")
        return

    if event_type in INGEST_EVENTS:
        print(f"노션 웹훅 {event_type} {ingest_page(entity['id'])}")


@router.post("/knowledge/notion/events")
async def handle_notion_event(
    request: Request,
    background: BackgroundTasks,
    x_notion_signature: str | None = Header(None, alias="X-Notion-Signature"),
) -> dict[str, str]:
    """노션 웹훅을 받습니다.

    Args:
        request: 서명 검증에 본문 원본이 필요해 직접 읽습니다
        background: 응답을 보낸 뒤 적재를 돌릴 자리
        x_notion_signature: 본문을 verification_token으로 서명한 HMAC-SHA256

    Returns:
        dict[str, str]: 받았다는 표시
    """
    body = await request.body()
    event = await request.json()

    token = event.get("verification_token")
    if token:
        print(f"노션 웹훅 verification_token: {token}")
        return {"status": "verified"}

    if not verify_signature(
        body, x_notion_signature, os.environ["NOTION_WEBHOOK_VERIFICATION_TOKEN"]
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="서명이 맞지 않습니다"
        )

    background.add_task(process_event, event)
    return {"status": "accepted"}
