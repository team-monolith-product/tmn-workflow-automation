"""
수집 채널 등록의 Service Layer입니다.

data_source 테이블이 SOT입니다. 등록 인터페이스는 WA 봇 멘션 도구
하나이고, 이 모듈은 그 도구가 실행하는 DB 조작만 담습니다.
"""

from typing import Any

import psycopg

UPSERT_CHANNEL = """
INSERT INTO data_source (source, external_id, name, enabled)
VALUES ('slack', %(channel_id)s, %(name)s, true)
ON CONFLICT (source, external_id) DO UPDATE SET
    name    = EXCLUDED.name,
    enabled = true
RETURNING id
"""

DISABLE_CHANNEL = """
UPDATE data_source SET enabled = false
WHERE source = 'slack' AND external_id = %(channel_id)s
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


def upsert_channel(conn: psycopg.Connection, channel_id: str, name: str) -> int:
    """채널을 등록하거나 재활성화합니다.

    joined_at은 쓰지 않습니다. 멘션 인터페이스에서 멤버십은 등록의
    전제조건이라 따로 기록할 사건이 아니고, 등록 시각은 created_at이
    이미 기록합니다.

    Args:
        conn: 커넥션
        channel_id: Slack 채널 ID
        name: 채널 이름

    Returns:
        int: data_source.id
    """
    row = conn.execute(
        UPSERT_CHANNEL, {"channel_id": channel_id, "name": name}
    ).fetchone()
    return row["id"]


def disable_channel(conn: psycopg.Connection, channel_id: str) -> int | None:
    """채널 수집을 내립니다. 봇 멤버십은 건드리지 않습니다.

    Args:
        conn: 커넥션
        channel_id: Slack 채널 ID

    Returns:
        int | None: data_source.id. 등록된 적 없으면 None
    """
    row = conn.execute(DISABLE_CHANNEL, {"channel_id": channel_id}).fetchone()
    return row["id"] if row else None
