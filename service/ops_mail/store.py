"""
ops@ 메일 기록을 읽고 씁니다.

메일 한 통은 ops_mail_message 한 행이고, 상대방 하나에 슬랙 스레드 하나는
ops_mail_thread 한 행입니다. 메일은 스레드를 모릅니다 -- 스레드는 상대방 주소로
찾습니다. 그래서 스레드가 열리기 전에 보낸 메일도, 열린 뒤에 채널에서 새로 보낸
메일도 따로 묶지 않아도 그 상대의 스레드에서 보입니다.
"""

import asyncio
from dataclasses import asdict, dataclass, fields
from typing import Any

import psycopg

from service.db import connect, fetch_all


@dataclass(frozen=True)
class OpsMailRow:
    """ops_mail_message 한 행. 메일 한 통이 한 행입니다."""

    direction: str
    message_id: str
    ses_message_id: str
    counterpart: str
    from_address: str
    to_addresses: list[str]
    subject: str
    body_text: str
    references_ids: list[str]
    s3_key: str | None
    channel_id: str
    sent_by: str | None


# 열 이름은 행 모양에서 딴다. 손으로 나열하면 필드를 추가했을 때 INSERT 가
# 조용히 그 열을 빠뜨린다.
COLUMNS = tuple(field.name for field in fields(OpsMailRow))

INSERT = f"""
INSERT INTO ops_mail_message ({", ".join(COLUMNS)})
VALUES ({", ".join(f"%({name})s" for name in COLUMNS)})
ON CONFLICT (ses_message_id) DO NOTHING
"""

PROCESSED_KEYS = "SELECT s3_key FROM ops_mail_message WHERE s3_key = ANY(%s)"

FIND_THREAD = """
SELECT thread_ts FROM ops_mail_thread WHERE channel_id = %s AND counterpart = %s
"""

# 먼저 연 쪽이 이긴다. 진 쪽은 이긴 스레드를 받아 쓴다.
CLAIM_THREAD = """
INSERT INTO ops_mail_thread (channel_id, counterpart, thread_ts)
VALUES (%s, %s, %s)
ON CONFLICT (channel_id, counterpart) DO NOTHING
"""

# 스레드가 열리기 전에 이 상대에게 보낸 메일. 스레드 첫 메시지에 보여준다.
SENT_TO = """
SELECT subject, sent_by, created_at
FROM ops_mail_message
WHERE channel_id = %s AND counterpart = %s AND direction = 'out'
ORDER BY created_at
"""

LATEST_IN_THREAD = """
SELECT m.*
FROM ops_mail_thread t
JOIN ops_mail_message m
  ON m.channel_id = t.channel_id AND m.counterpart = t.counterpart
WHERE t.channel_id = %s AND t.thread_ts = %s
ORDER BY m.created_at DESC
LIMIT 1
"""


async def run(function, *args):
    """store 함수 하나를 새 커넥션으로 부릅니다. psycopg 는 동기라 스레드로 넘깁니다."""

    def call():
        with connect() as conn:
            return function(conn, *args)

    return await asyncio.to_thread(call)


def processed_keys(conn: psycopg.Connection, keys: list[str]) -> set[str]:
    """S3 원본 키 중 이미 처리해서 기록된 것을 고릅니다.

    Args:
        conn: 커넥션
        keys: S3 객체 키

    Returns:
        set[str]: 기록돼 있는 키
    """
    return {row["s3_key"] for row in fetch_all(conn, PROCESSED_KEYS, (keys,))}


def find_thread(
    conn: psycopg.Connection, channel_id: str, counterpart: str
) -> str | None:
    """상대방 스레드를 찾습니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        str | None: 스레드 ts. 아직 스레드가 없는 상대면 None
    """
    rows = fetch_all(conn, FIND_THREAD, (channel_id, counterpart))
    return rows[0]["thread_ts"] if rows else None


def claim_thread(
    conn: psycopg.Connection, channel_id: str, counterpart: str, thread_ts: str
) -> str:
    """새로 연 스레드를 상대방 스레드로 등록합니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)
        thread_ts: 새로 연 스레드 ts

    Returns:
        str: 상대방 스레드 ts. 다른 쪽이 먼저 열었으면 그 스레드입니다.
    """
    with conn.cursor() as cur:
        cur.execute(CLAIM_THREAD, (channel_id, counterpart, thread_ts))
    return find_thread(conn, channel_id, counterpart)


def sent_to(
    conn: psycopg.Connection, channel_id: str, counterpart: str
) -> list[dict[str, Any]]:
    """이 상대에게 보낸 메일을 찾습니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        list[dict[str, Any]]: 제목·보낸 사람·보낸 시각
    """
    return fetch_all(conn, SENT_TO, (channel_id, counterpart))


def latest_in_thread(
    conn: psycopg.Connection, channel_id: str, thread_ts: str
) -> dict[str, Any] | None:
    """상대방 스레드의 상대와 마지막으로 오간 메일을 찾습니다. 답장은 이 메일에 겁니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        thread_ts: 스레드 ts

    Returns:
        dict[str, Any] | None: 행. 상대방 스레드가 아니면 None
    """
    rows = fetch_all(conn, LATEST_IN_THREAD, (channel_id, thread_ts))
    return rows[0] if rows else None


def insert(conn: psycopg.Connection, row: OpsMailRow) -> None:
    """메일 한 통을 남깁니다.

    Args:
        conn: 커넥션
        row: 남길 행
    """
    with conn.cursor() as cur:
        cur.execute(INSERT, asdict(row))
