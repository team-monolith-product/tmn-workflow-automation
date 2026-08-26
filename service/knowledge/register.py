"""
수집 대상 등록의 Service Layer입니다.

data_source 테이블이 SOT입니다. 무엇을 수집할지는 이 표에 행이 있느냐로만
정해집니다. 슬랙은 채널 하나가 행 하나이고, 노션은 루트 페이지 하나가 행
하나입니다. 소스마다 등록 인터페이스는 다르지만 DB 조작은 같습니다.
"""

import json
from typing import Any

import psycopg

UPSERT_SOURCE = """
INSERT INTO data_source (source, external_id, name, config, enabled)
VALUES (%(source)s, %(external_id)s, %(name)s, %(config)s, true)
ON CONFLICT (source, external_id) DO UPDATE SET
    name    = EXCLUDED.name,
    config  = EXCLUDED.config,
    enabled = true
RETURNING id
"""

DISABLE_SOURCE = """
UPDATE data_source SET enabled = false
WHERE source = %(source)s AND external_id = %(external_id)s
RETURNING id
"""


def validate_public_channel(info: dict[str, Any]) -> str | None:
    """수집 가능한 채널인지 검사합니다.

    공개 채널만 수집합니다. DM에 API 키가 평문으로 오간 사례를 확인했고,
    봇이 멤버인 비공개 채널에서도 멘션 이벤트는 들어오므로 여기서 막습니다.

    Args:
        info: conversations.info 응답의 channel 객체

    Returns:
        str | None: 거절 사유. 수집 가능하면 None
    """
    if info.get("is_private") or not info.get("is_channel"):
        return "공개 채널만 수집할 수 있습니다."
    return None


def upsert_source(
    conn: psycopg.Connection,
    source: str,
    external_id: str,
    name: str,
    config: dict[str, Any] | None = None,
) -> int:
    """수집 대상을 등록하거나 재활성화합니다.

    joined_at은 쓰지 않습니다. 슬랙 멘션 인터페이스에서 멤버십은 등록의
    전제조건이라 따로 기록할 사건이 아니고, 등록 시각은 created_at이
    이미 기록합니다.

    Args:
        conn: 커넥션
        source: "slack" 또는 "notion"
        external_id: 소스 안에서 대상을 가리키는 ID
        name: 사람이 읽는 이름
        config: 소스별 수집 설정

    Returns:
        int: data_source.id
    """
    row = conn.execute(
        UPSERT_SOURCE,
        {
            "source": source,
            "external_id": external_id,
            "name": name,
            "config": json.dumps(config or {}, ensure_ascii=False),
        },
    ).fetchone()
    return row["id"]


def disable_source(
    conn: psycopg.Connection, source: str, external_id: str
) -> int | None:
    """수집을 내립니다. 슬랙 봇 멤버십이나 노션 공유 설정은 건드리지 않습니다.

    Args:
        conn: 커넥션
        source: "slack" 또는 "notion"
        external_id: 소스 안에서 대상을 가리키는 ID

    Returns:
        int | None: data_source.id. 등록된 적 없으면 None
    """
    row = conn.execute(
        DISABLE_SOURCE, {"source": source, "external_id": external_id}
    ).fetchone()
    return row["id"] if row else None
