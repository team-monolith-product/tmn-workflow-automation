"""
채널과 참가자 스프레드시트를 잇습니다.

지식 수집 채널 등록과 같은 모양입니다. 채널에서 봇을 멘션해 연결하고,
그 뒤로는 그 채널에서 보내는 문자가 그 시트의 '발송이력' 탭에 쌓입니다.

매핑이 없으면 발송을 막습니다. 기본 시트로 떨어뜨리면 어느 사업 이력이
어디 쌓였는지 아무도 모르게 되고, 그건 잘못된 시트에 적히는 것보다 나쁩니다.
"""

import re
from typing import Any

import psycopg

from service.knowledge.db import connect

UPSERT = """
INSERT INTO channel_sheet (channel_id, spreadsheet_id, connected_by)
VALUES (%(channel_id)s, %(spreadsheet_id)s, %(connected_by)s)
ON CONFLICT (channel_id) DO UPDATE SET
    spreadsheet_id = EXCLUDED.spreadsheet_id,
    connected_by   = EXCLUDED.connected_by,
    connected_at   = now()
RETURNING spreadsheet_id
"""

SELECT_ONE = "SELECT spreadsheet_id FROM channel_sheet WHERE channel_id = %(id)s"

DELETE_ONE = "DELETE FROM channel_sheet WHERE channel_id = %(id)s RETURNING channel_id"

# https://docs.google.com/spreadsheets/d/<id>/edit 또는 id 자체
_ID_IN_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_BARE_ID = re.compile(r"^[a-zA-Z0-9-_]{20,}$")


class NotConnected(RuntimeError):
    """채널에 참가자 시트가 연결되지 않았을 때 발생합니다."""


def parse_spreadsheet_id(value: str) -> str:
    """스프레드시트 주소나 ID 에서 ID 를 뽑습니다.

    사람은 보통 주소창을 통째로 붙여넣습니다.

    Args:
        value: 스프레드시트 URL 또는 ID

    Returns:
        str: 스프레드시트 ID

    Raises:
        ValueError: 어느 쪽으로도 읽히지 않을 때
    """
    value = value.strip()
    found = _ID_IN_URL.search(value)
    if found:
        return found.group(1)
    if _BARE_ID.match(value):
        return value
    raise ValueError(f"스프레드시트 주소나 ID 로 읽히지 않습니다: {value}")


def connect_sheet(
    conn: psycopg.Connection, channel_id: str, spreadsheet_id: str, connected_by: str
) -> str:
    """채널에 참가자 시트를 연결합니다. 이미 있으면 바꿉니다.

    Args:
        conn: 커넥션
        channel_id: 슬랙 채널 ID
        spreadsheet_id: 스프레드시트 ID
        connected_by: 연결한 사람 이메일

    Returns:
        str: 연결된 스프레드시트 ID
    """
    row = conn.execute(
        UPSERT,
        {
            "channel_id": channel_id,
            "spreadsheet_id": spreadsheet_id,
            "connected_by": connected_by,
        },
    ).fetchone()
    conn.commit()
    return row["spreadsheet_id"]


def disconnect_sheet(conn: psycopg.Connection, channel_id: str) -> str | None:
    """채널의 시트 연결을 끊습니다.

    Args:
        conn: 커넥션
        channel_id: 슬랙 채널 ID

    Returns:
        str | None: 끊은 채널 ID. 연결된 적 없으면 None
    """
    row = conn.execute(DELETE_ONE, {"id": channel_id}).fetchone()
    conn.commit()
    return row["channel_id"] if row else None


def sheet_for(channel_id: str) -> str:
    """채널에 연결된 스프레드시트 ID 를 찾습니다.

    Args:
        channel_id: 슬랙 채널 ID

    Returns:
        str: 스프레드시트 ID

    Raises:
        NotConnected: 연결된 시트가 없을 때
    """
    with connect() as conn:
        row = conn.execute(SELECT_ONE, {"id": channel_id}).fetchone()
    if row is None:
        raise NotConnected(
            "이 채널에 참가자 시트가 연결되어 있지 않습니다. "
            "`@봇 이 채널에 <스프레드시트 주소> 연결해줘` 로 먼저 연결하세요."
        )
    return row["spreadsheet_id"]


def describe(channel_id: str) -> dict[str, Any] | None:
    """연결 상태를 조회합니다.

    Args:
        channel_id: 슬랙 채널 ID

    Returns:
        dict[str, Any] | None: 연결 정보. 없으면 None
    """
    with connect() as conn:
        return conn.execute(SELECT_ONE, {"id": channel_id}).fetchone()
