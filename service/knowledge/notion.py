"""
노션 페이지를 지식베이스 item 행으로 정규화하는 Service Layer입니다.

페이지 하나가 행 하나입니다. 슬랙에서 스레드 하나를 행 하나로 둔 것과 같은
결입니다. 읽는 사람이 통째로 여는 단위가 곧 검색해서 찾고 싶은 단위입니다.

수집 범위는 통합에 부여된 콘텐츠 사용 권한 전체입니다. 등록 목록을 따로
관리하지 않습니다. 노션 연결 설정에서 페이지를 넣고 빼는 것이 곧 수집 범위
변경이고, 두 곳을 맞춰야 하는 상태를 만들지 않습니다. 슬랙이 data_source
등록을 SOT로 두는 것과 다른데, 슬랙은 봇이 채널 멤버인 것과 수집 대상인 것이
별개라 따로 표시할 곳이 필요했고 노션은 권한 자체가 그 표시입니다.

search는 질의 없이 부르면 통합에 공유된 페이지와 데이터소스를 전부 돌려줍니다.
그래서 무엇이 권한 안에 있는지를 API로 알 수 있고, 사라진 것도 알 수 있습니다.
"""

import json
from datetime import datetime, timedelta
from typing import Any

from notion_client import Client

from service.knowledge.ingest import compute_content_hash

# 노션이 페이지 제목을 담는 프로퍼티는 타입이 "title"인 것 하나뿐이고 이름은
# 데이터베이스마다 다르다. 이름으로 찾을 수 없어 타입으로 찾는다.
TITLE_TYPE = "title"

PAGE_SIZE = 100


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
        response = client.users.list(page_size=PAGE_SIZE, start_cursor=cursor)
        for user in response["results"]:
            email = (user.get("person") or {}).get("email")
            if email:
                emails[user["id"]] = email
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return emails


def fetch_accessible(client: Client) -> list[dict[str, Any]]:
    """통합이 접근할 수 있는 페이지와 데이터소스를 전부 받아옵니다.

    데이터소스까지 받는 이유는 부모 사슬을 잇기 위해서입니다. 데이터베이스
    안의 행은 부모가 데이터소스라, 페이지만 받으면 사슬이 거기서 끊겨 행마다
    자기가 최상위인 것처럼 보입니다.

    Args:
        client: 노션 클라이언트

    Returns:
        list[dict[str, Any]]: page와 data_source 객체
    """
    results: list[dict[str, Any]] = []
    cursor = None
    while True:
        response = client.search(page_size=PAGE_SIZE, start_cursor=cursor)
        results.extend(response["results"])
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return results


def parent_id(node: dict[str, Any]) -> str | None:
    """부모를 가리키는 ID를 꺼냅니다.

    부모 종류마다 키 이름이 달라서(page_id·data_source_id·database_id·block_id)
    type이 지시하는 키를 읽습니다. 워크스페이스 직속이면 부모가 없습니다.

    Args:
        node: 노션 page 또는 data_source 객체

    Returns:
        str | None: 부모 ID. 워크스페이스 직속이면 None
    """
    parent = node.get("parent") or {}
    return parent.get(parent.get("type", ""))


def derive_roots(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """각 노드가 어느 최상위 항목에 속하는지 정합니다.

    부모를 따라 올라가다가 접근 권한 밖으로 나가면 거기서 멈춥니다. 멈춘
    자리가 곧 연결 설정에서 권한을 준 항목입니다. 그 위는 통합이 볼 수 없어
    search에 나오지 않습니다.

    사슬이 도는 경우는 없지만, 노션이 예상 못 한 부모를 주더라도 무한히 돌지
    않도록 지나온 자리를 기억합니다.

    Args:
        nodes: fetch_accessible 결과

    Returns:
        dict[str, str]: 노드 ID → 최상위 항목 ID
    """
    by_id = {node["id"]: node for node in nodes}

    roots: dict[str, str] = {}
    for node in nodes:
        path = []
        current = node["id"]
        while current not in roots and current not in path:
            path.append(current)
            parent = parent_id(by_id[current])
            if parent not in by_id:
                break
            current = parent

        root = roots.get(current, current)
        for node_id in path:
            roots[node_id] = root
    return roots


def node_title(node: dict[str, Any]) -> str:
    """페이지나 데이터소스의 제목을 뽑습니다.

    데이터소스는 title이 프로퍼티가 아니라 최상위 필드입니다.

    Args:
        node: 노션 page 또는 data_source 객체

    Returns:
        str: 제목. 비어 있으면 "제목 없음"
    """
    if node.get("object") == "data_source":
        text = "".join(part["plain_text"] for part in node.get("title", []))
        return text[:200] or "제목 없음"

    for prop in node.get("properties", {}).values():
        if prop.get("type") == TITLE_TYPE:
            text = "".join(part["plain_text"] for part in prop[TITLE_TYPE])
            return text[:200] or "제목 없음"
    return "제목 없음"


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


def build_page_row(
    data_source_id: int,
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
        data_source_id: 최상위 항목에 대응하는 data_source.id
        page: 노션 page 객체
        markdown: 페이지 본문을 마크다운으로 바꾼 것
        distill_delay_seconds: 마지막 편집 이후 정제를 미룰 시간
        user_emails: 노션 사용자 ID → 이메일

    Returns:
        dict[str, Any]: UPSERT_ITEM 바인딩 파라미터
    """
    title = node_title(page)
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
