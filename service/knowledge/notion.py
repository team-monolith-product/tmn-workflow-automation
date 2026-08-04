"""
노션 페이지를 지식베이스 item 행으로 정규화하는 Service Layer입니다.

페이지 하나가 행 하나입니다. 슬랙에서 스레드 하나를 행 하나로 둔 것과 같은
결입니다. 읽는 사람이 통째로 여는 단위가 곧 검색해서 찾고 싶은 단위입니다.

수집 범위는 등록한 루트 페이지와 그 아래 하위 페이지 전부입니다. 통합에
공유된 것을 전부 훑지 않습니다. 무엇을 수집할지는 data_source에 행이 있느냐로만
정해야 하는데, search로 훑으면 공유 설정을 바꾼 사람이 모르는 사이에 수집 범위가
바뀝니다.

작성자는 이메일로 적습니다. 슬랙과 같은 사람을 같은 문자열로 가리켜야 합니다.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Iterator

from notion_client import Client

from service.knowledge.ingest import compute_content_hash

# 노션이 페이지 제목을 담는 프로퍼티는 타입이 "title"인 것 하나뿐이고 이름은
# 데이터베이스마다 다르다. 이름으로 찾을 수 없어 타입으로 찾는다.
TITLE_TYPE = "title"

# 계층을 만드는 블록. 토글이나 컬럼 안에 하위 페이지를 넣는 사람이 있어
# 자식 블록까지 따라 내려간다.
CONTAINER_TYPES = {
    "toggle",
    "column_list",
    "column",
    "bulleted_list_item",
    "numbered_list_item",
    "callout",
    "quote",
    "synced_block",
}


def fetch_user_emails(client: Client) -> dict[str, str]:
    """노션 사용자 ID를 이메일로 바꾸는 매핑을 만듭니다.

    Args:
        client: 노션 클라이언트

    Returns:
        dict[str, str]: 이메일이 있는 사람 계정만 담은 매핑. 봇은 빠진다
    """
    emails: dict[str, str] = {}
    cursor = None
    while True:
        response = client.users.list(page_size=100, start_cursor=cursor)
        for user in response["results"]:
            email = (user.get("person") or {}).get("email")
            if email:
                emails[user["id"]] = email
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return emails


def author_of(actor: dict[str, Any], user_emails: dict[str, str]) -> str:
    """created_by·last_edited_by를 소스 간에 통하는 식별자로 바꿉니다.

    Args:
        actor: 노션 user 참조 객체
        user_emails: 노션 사용자 ID → 이메일

    Returns:
        str: 이메일. 사람이 아니거나 매핑에 없으면 "bot:<id>"
    """
    user_id = actor.get("id", "")
    return user_emails.get(user_id) or f"bot:{user_id or 'unknown'}"


def page_title(page: dict[str, Any]) -> str:
    """페이지 제목을 뽑습니다.

    Args:
        page: 노션 page 객체

    Returns:
        str: 제목. 비어 있으면 "제목 없음"
    """
    for prop in page.get("properties", {}).values():
        if prop.get("type") == TITLE_TYPE:
            text = "".join(part["plain_text"] for part in prop[TITLE_TYPE])
            return text[:200] or "제목 없음"
    return "제목 없음"


def walk_page_ids(client: Client, root_id: str) -> Iterator[str]:
    """루트 페이지와 그 아래 모든 하위 페이지 ID를 훑습니다.

    같은 페이지를 두 번 내지 않습니다. synced_block은 원본을 여러 곳에서
    가리키므로 그냥 따라가면 같은 페이지가 반복됩니다.

    Args:
        client: 노션 클라이언트
        root_id: 등록한 루트 페이지 ID

    Yields:
        str: 페이지 ID. 루트부터 나온다
    """
    seen: set[str] = set()
    queue = [root_id]
    while queue:
        page_id = queue.pop(0)
        if page_id in seen:
            continue
        seen.add(page_id)
        yield page_id
        queue.extend(_child_page_ids(client, page_id))


def _child_page_ids(client: Client, block_id: str) -> list[str]:
    """블록 아래에 있는 하위 페이지 ID를 모읍니다.

    Args:
        client: 노션 클라이언트
        block_id: 페이지 또는 컨테이너 블록 ID

    Returns:
        list[str]: 하위 페이지 ID
    """
    found: list[str] = []
    cursor = None
    while True:
        response = client.blocks.children.list(
            block_id=block_id, page_size=100, start_cursor=cursor
        )
        for block in response["results"]:
            block_type = block["type"]
            if block_type == "child_page":
                found.append(block["id"])
            elif block_type in CONTAINER_TYPES and block.get("has_children"):
                found.extend(_child_page_ids(client, block["id"]))
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return found


def build_page_row(
    data_source_id: int,
    root_id: str,
    page: dict[str, Any],
    markdown: str,
    distill_delay_seconds: int,
    user_emails: dict[str, str],
) -> dict[str, Any]:
    """노션 페이지를 item 행으로 정규화합니다.

    raw_text는 마크다운입니다. 어휘 검색 대상이라 원문 표기를 최대한 남겨야
    하는데, 블록 JSON을 그대로 이으면 타입 이름과 서식 속성이 본문만큼
    섞여 들어옵니다.

    Args:
        data_source_id: 루트 페이지에 대응하는 data_source.id
        root_id: 등록한 루트 페이지 ID
        page: 노션 page 객체
        markdown: 페이지 본문을 마크다운으로 바꾼 것
        distill_delay_seconds: 마지막 편집 이후 정제를 미룰 시간
        user_emails: 노션 사용자 ID → 이메일

    Returns:
        dict[str, Any]: UPSERT_ITEM 바인딩 파라미터
    """
    title = page_title(page)
    last_edited = datetime.fromisoformat(page["last_edited_time"])

    # 제목을 본문 앞에 붙인다. 제목에만 있는 말이 흔한데 raw_text에 없으면
    # 어휘 검색으로는 영영 걸리지 않는다.
    raw_text = f"{title}\n\n{markdown}"

    return {
        "data_source_id": data_source_id,
        "external_id": page["id"],
        "url": page["url"],
        "title": title,
        "author": author_of(page.get("created_by", {}), user_emails),
        "source_created_at": datetime.fromisoformat(page["created_time"]),
        "source_updated_at": last_edited,
        "raw": json.dumps(page, ensure_ascii=False),
        "raw_text": raw_text,
        "metadata": json.dumps(
            {
                "root_page_id": root_id,
                "parent": page.get("parent", {}),
                "last_edited_by": author_of(
                    page.get("last_edited_by", {}), user_emails
                ),
            },
            ensure_ascii=False,
        ),
        "content_hash": compute_content_hash(raw_text),
        # 노션에는 봇 단독 스레드에 대응하는 것이 없다. 페이지는 사람이 쓴다.
        "distill_state": "pending",
        "distill_after": last_edited + timedelta(seconds=distill_delay_seconds),
    }
