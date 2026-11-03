"""
ops@ 메일 기록을 읽고 씁니다.

슬랙 스레드는 상대방 주소 하나에 하나입니다. 같은 상대와 주고받은 메일은
방향과 상관없이 같은 스레드에 쌓입니다.

새 메일로 먼저 보낸 상대는 회신이 오기 전까지 스레드가 없습니다(thread_ts 가
NULL). 수백 명에게 보내도 상대마다 스레드를 열지 않기 위해서입니다. 회신이 와서
스레드가 열리면 그때 그 상대의 메일들을 스레드에 묶습니다.

psycopg 는 동기라 봇 쪽에서는 스레드로 넘겨 부릅니다.
"""

from dataclasses import asdict, dataclass, fields
from typing import Any

import psycopg

from service.db import fetch_all


@dataclass(frozen=True)
class OpsMailRow:
    """ops_mail_message 한 행. 메일 한 통이 한 행입니다."""

    direction: str
    message_id: str
    ses_message_id: str | None
    counterpart: str
    from_address: str
    to_addresses: list[str]
    subject: str
    body_text: str
    references_ids: list[str]
    s3_key: str | None
    channel_id: str
    thread_ts: str | None
    sent_by: str | None


# 열 이름은 행 모양에서 딴다. 손으로 나열하면 필드를 추가했을 때 INSERT 가
# 조용히 그 열을 빠뜨린다.
COLUMNS = tuple(field.name for field in fields(OpsMailRow))

INSERT = f"""
INSERT INTO ops_mail_message ({", ".join(COLUMNS)})
VALUES ({", ".join(f"%({name})s" for name in COLUMNS)})
ON CONFLICT (message_id) DO NOTHING
"""

EXISTS = "SELECT 1 FROM ops_mail_message WHERE message_id = %s"

# 상대방 스레드는 처음 만들어진 것을 계속 쓴다.
FIND_THREAD = """
SELECT thread_ts
FROM ops_mail_message
WHERE channel_id = %s AND counterpart = %s AND thread_ts IS NOT NULL
ORDER BY created_at
LIMIT 1
"""

# 스레드가 열리기 전에 이 상대에게 보낸 메일. 스레드 첫 메시지에 보여준다.
UNTHREADED = """
SELECT subject, sent_by, created_at
FROM ops_mail_message
WHERE channel_id = %s AND counterpart = %s AND thread_ts IS NULL
ORDER BY created_at
"""

ATTACH_THREAD = """
UPDATE ops_mail_message
SET thread_ts = %s
WHERE channel_id = %s AND counterpart = %s AND thread_ts IS NULL
"""

LATEST_IN_THREAD = """
SELECT *
FROM ops_mail_message
WHERE channel_id = %s AND thread_ts = %s
ORDER BY created_at DESC
LIMIT 1
"""


def exists(conn: psycopg.Connection, message_id: str) -> bool:
    """이미 기록된 메일인지 봅니다. SNS 는 같은 알림을 두 번 보낼 수 있습니다.

    Args:
        conn: 커넥션
        message_id: Message-ID

    Returns:
        bool: 기록돼 있으면 True
    """
    return bool(fetch_all(conn, EXISTS, (message_id,)))


def find_thread(
    conn: psycopg.Connection, channel_id: str, counterpart: str
) -> str | None:
    """상대방 스레드를 찾습니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        str | None: 스레드 ts. 처음 오가는 상대면 None
    """
    rows = fetch_all(conn, FIND_THREAD, (channel_id, counterpart))
    return rows[0]["thread_ts"] if rows else None


def unthreaded(
    conn: psycopg.Connection, channel_id: str, counterpart: str
) -> list[dict[str, Any]]:
    """스레드가 열리기 전에 이 상대에게 보낸 메일을 찾습니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        list[dict[str, Any]]: 제목·보낸 사람·보낸 시각
    """
    return fetch_all(conn, UNTHREADED, (channel_id, counterpart))


def attach_thread(
    conn: psycopg.Connection, channel_id: str, counterpart: str, thread_ts: str
) -> None:
    """스레드가 없던 이 상대의 메일들을 새로 연 스레드에 묶습니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)
        thread_ts: 새로 연 스레드 ts
    """
    with conn.cursor() as cur:
        cur.execute(ATTACH_THREAD, (thread_ts, channel_id, counterpart))


def latest_in_thread(
    conn: psycopg.Connection, channel_id: str, thread_ts: str
) -> dict[str, Any] | None:
    """스레드에서 마지막으로 오간 메일을 찾습니다. 답장은 이 메일에 겁니다.

    Args:
        conn: 커넥션
        channel_id: 메일 채널 ID
        thread_ts: 스레드 ts

    Returns:
        dict[str, Any] | None: 행. 메일 스레드가 아니면 None
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
