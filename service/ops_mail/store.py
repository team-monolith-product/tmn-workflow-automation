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

# 받은 메일을 맡을 수 있는 횟수와, 맡은 쪽이 끝내기를 기다리는 시간.
# 기다리는 동안 다른 쪽(SNS 재전송, 정리 작업)은 같은 메일을 맡지 못한다.
MAX_ATTEMPTS = 3
LEASE = "5 minutes"

_VALUES = ", ".join(f"%({name})s" for name in COLUMNS)

# 보낸 메일. 슬랙에 따로 올릴 것이 없어 넣을 때 처리가 끝난다.
INSERT = f"""
INSERT INTO ops_mail_message ({", ".join(COLUMNS)}, processed_at)
VALUES ({_VALUES}, now())
ON CONFLICT (ses_message_id) DO NOTHING
"""

# 받은 메일을 맡는다. 처음이면 넣고, 이미 있으면 아직 안 끝났고 횟수가 남았고
# 다른 쪽이 맡은 지 LEASE 가 지났을 때만 다시 맡는다. 맡았으면 행이 돌아온다.
CLAIM = f"""
INSERT INTO ops_mail_message ({", ".join(COLUMNS)}, attempts, claimed_at)
VALUES ({_VALUES}, 1, now())
ON CONFLICT (ses_message_id) DO UPDATE
SET attempts = ops_mail_message.attempts + 1, claimed_at = now()
WHERE ops_mail_message.processed_at IS NULL
  AND ops_mail_message.attempts < {MAX_ATTEMPTS}
  AND ops_mail_message.claimed_at < now() - interval '{LEASE}'
RETURNING id
"""

MARK_PROCESSED = """
UPDATE ops_mail_message SET processed_at = now() WHERE ses_message_id = %s
"""

# 정리 작업이 더 볼 필요 없는 원본: 처리가 끝났거나, 횟수를 다 썼다.
SETTLED_KEYS = f"""
SELECT s3_key FROM ops_mail_message
WHERE s3_key = ANY(%s)
  AND (processed_at IS NOT NULL OR attempts >= {MAX_ATTEMPTS})
"""

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


def settled_keys(conn: psycopg.Connection, keys: list[str]) -> set[str]:
    """S3 원본 키 중 더 처리할 필요가 없는 것을 고릅니다.

    Args:
        conn: 커넥션
        keys: S3 객체 키

    Returns:
        set[str]: 처리가 끝났거나 횟수를 다 쓴 키
    """
    return {row["s3_key"] for row in fetch_all(conn, SETTLED_KEYS, (keys,))}


def claim(conn: psycopg.Connection, row: OpsMailRow) -> bool:
    """받은 메일 하나를 이쪽이 처리하겠다고 자리를 잡습니다.

    Args:
        conn: 커넥션
        row: 받은 메일 행

    Returns:
        bool: 맡았으면 True. 이미 끝났거나, 다른 쪽이 처리 중이거나, 횟수를 다 썼으면 False
    """
    return bool(fetch_all(conn, CLAIM, asdict(row)))


def mark_processed(conn: psycopg.Connection, ses_message_id: str) -> None:
    """받은 메일을 슬랙에 다 올렸다고 표시합니다.

    Args:
        conn: 커넥션
        ses_message_id: SES 메시지 ID
    """
    with conn.cursor() as cur:
        cur.execute(MARK_PROCESSED, (ses_message_id,))


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
    """보낸 메일 한 통을 남깁니다.

    Args:
        conn: 커넥션
        row: 남길 행
    """
    with conn.cursor() as cur:
        cur.execute(INSERT, asdict(row))
