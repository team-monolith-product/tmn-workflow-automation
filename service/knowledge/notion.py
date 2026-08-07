"""
노션 페이지를 지식베이스 item 행으로 정규화하는 Service Layer입니다.

페이지 하나가 행 하나입니다. 슬랙에서 스레드 하나를 행 하나로 둔 것과 같은
결입니다. 읽는 사람이 통째로 여는 단위가 곧 검색해서 찾고 싶은 단위입니다.

수집 범위는 통합에 부여된 콘텐츠 사용 권한 전체입니다. 등록 목록을 따로
관리하지 않습니다. 노션 연결 설정에서 페이지를 넣고 빼는 것이 곧 수집 범위
변경이고, 두 곳을 맞춰야 하는 상태를 만들지 않습니다. 슬랙이 data_source
등록을 SOT로 두는 것과 다른데, 슬랙은 봇이 채널 멤버인 것과 수집 대상인 것이
별개라 따로 표시할 곳이 필요했고 노션은 권한 자체가 그 표시입니다.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from notion_client import Client

from service.knowledge.ingest import compute_content_hash

# 노션이 페이지 제목을 담는 프로퍼티는 타입이 "title"인 것 하나뿐이고 이름은
# 데이터베이스마다 다르다. 이름으로 찾을 수 없어 타입으로 찾는다.
TITLE_TYPE = "title"

PAGE_SIZE = 100

# 본문이 이보다 짧은 데이터베이스 행은 문서가 아니라 데이터로 본다.
#
# 실측(2026-08-05, 표본 125건)에서 두 부류가 깨끗하게 갈렸다. 구매 내역·결산
# 자료·학교 목록 같은 표는 본문 중앙값이 0자이고 표본 전부가 이 값에 못 미쳤다.
# 회의록·일반 문서·출장보고서는 중앙값이 4,600~13,200자였다. 이름을 코드에
# 적어두지 않아도 길이만으로 갈린다.
MIN_BODY_CHARS = 100


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


def parent_ref(node: dict[str, Any]) -> tuple[str, str] | None:
    """부모를 (종류, ID)로 꺼냅니다.

    부모 종류마다 키 이름이 달라서(page_id·data_source_id·database_id·block_id)
    type이 지시하는 키를 읽습니다.

    Args:
        node: 노션 page·data_source·database·block 객체

    Returns:
        tuple[str, str] | None: (부모 종류, 부모 ID). 워크스페이스 직속이면 None.
            워크스페이스 부모는 값이 ID가 아니라 true라 ID로 쓸 수 없다
    """
    parent = node.get("parent") or {}
    kind = parent.get("type")
    if not kind or kind == "workspace":
        return None
    return kind, parent[kind]


def build_fetch_node(
    client: Client, cache: dict[str, dict[str, Any] | None]
) -> Callable[[str, str], dict[str, Any] | None]:
    """search에 나오지 않는 조상을 종류에 맞는 API로 받아오는 함수를 만듭니다.

    데이터소스가 여기 있는 이유는 search가 데이터소스를 다 돌려주지 않아서입니다.
    실측에서 행이 참조하는 470개 중 28개가 빠져 있었고 그 아래 5,182행이 딸려
    있었습니다.

    캐시를 호출자가 넘깁니다. 사슬을 잇느라 받아온 조상이 곧 최상위의 이름을
    찾을 곳이기도 해서입니다.

    Args:
        client: 노션 클라이언트
        cache: 노드 ID → 받아온 노드. 못 받았으면 None이 들어갑니다

    Returns:
        Callable[[str, str], dict[str, Any] | None]: derive_roots에 넘길 함수
    """
    retrieve = {
        "database_id": client.databases.retrieve,
        "data_source_id": client.data_sources.retrieve,
        "block_id": client.blocks.retrieve,
        "page_id": client.pages.retrieve,
    }

    def fetch_node(kind: str, node_id: str) -> dict[str, Any] | None:
        if node_id not in cache:
            get = retrieve.get(kind)
            try:
                cache[node_id] = get(node_id) if get else None
            except Exception:
                cache[node_id] = None
        return cache[node_id]

    return fetch_node


def is_database_row(node: dict[str, Any]) -> bool:
    """데이터베이스 안의 행인지 봅니다.

    Args:
        node: 노션 page 객체

    Returns:
        bool: 부모가 데이터소스면 True
    """
    return (node.get("parent") or {}).get("type") == "data_source_id"


def derive_roots(
    nodes: list[dict[str, Any]],
    fetch_node: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, str]:
    """각 노드가 어느 팀스페이스에 속하는지 정합니다.

    최상위는 워크스페이스 직속 페이지, 즉 연결 설정 화면에 보이는 팀스페이스로
    통일합니다. 부여한 권한 단위와 검색 결과의 출처가 일치해야 이해하기 쉽고,
    데이터베이스 이름은 "스터디"나 "가이드 문서"처럼 겹치는 것이 많아 출처로
    쓰기에 모호합니다.

    사슬이 두 군데서 끊깁니다. search가 page와 data_source만 돌려주기 때문입니다.

    - 데이터베이스 행의 부모는 데이터소스이고, 데이터소스의 부모는 데이터베이스다
    - 토글이나 컬럼 안에 있는 페이지의 부모는 블록이다

    둘 다 fetch_node로 잇습니다. 실측에서 "중요 문서"는 데이터소스 →
    데이터베이스 → 페이지 → 블록 → 블록 → 데이터베이스 → 페이지(보안)로
    여섯 단계였습니다.

    fetch_node는 부모 참조가 아니라 노드 자체를 돌려줍니다. 부모 참조만 받으면
    "조회 실패"와 "부모가 워크스페이스라 없음"이 똑같이 None이 되어 구분할 수
    없습니다. 실제로 "회의록"은 팀스페이스가 아니라 워크스페이스 직속
    데이터베이스인데, 이걸 실패로 보면 그 아래 466건이 엉뚱한 자리에 붙습니다.

    Args:
        nodes: 최상위를 정할 page·data_source 객체
        fetch_node: (종류, ID)를 받아 그 노드 객체를 돌려주는 함수. 못 찾으면
            None. 생략하면 사슬이 끊긴 자리에서 멈춥니다

    Returns:
        dict[str, str]: 노드 ID → 최상위 ID
    """
    by_id = {node["id"]: node for node in nodes}
    memo: dict[str, str] = {}

    def root_of(kind: str, node_id: str, seen: set[str], known: str) -> str:
        if node_id in memo:
            return memo[node_id]
        if node_id in seen:
            # 부모가 서로를 가리킨다. 노션이 이런 응답을 줄 일은 없지만
            # 무한히 돌 이유도 없다.
            return known
        seen.add(node_id)

        node = by_id.get(node_id)
        if node is None and fetch_node is not None:
            node = fetch_node(kind, node_id)
        if node is None:
            # 못 찾았다. 마지막으로 아는 노드가 최상위다. 모르는 블록 ID를
            # 최상위로 두면 이름 없는 data_source가 생긴다.
            return known

        above = parent_ref(node)
        result = root_of(*above, seen, node_id) if above else node_id
        memo[node_id] = result
        return result

    return {n["id"]: root_of("page_id", n["id"], set(), n["id"]) for n in nodes}


def node_title(node: dict[str, Any]) -> str:
    """페이지·데이터소스·데이터베이스의 제목을 뽑습니다.

    데이터소스와 데이터베이스는 title이 프로퍼티가 아니라 최상위 필드입니다.
    데이터베이스가 여기 있는 이유는 워크스페이스 직속 데이터베이스가 최상위가
    될 수 있어서입니다.

    Args:
        node: 노션 page·data_source·database 객체

    Returns:
        str: 제목. 비어 있으면 "제목 없음"
    """
    if node.get("object") in ("data_source", "database"):
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
