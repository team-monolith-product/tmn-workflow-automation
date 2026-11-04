"""
ops@ 메일 기록을 읽고 씁니다.

메일 한 통은 ops_mail_message 한 행이고, 상대방 하나에 슬랙 스레드 하나는
ops_mail_thread 한 행입니다. S3 에 들어온 받은 메일 원본의 처리 상태는
ops_mail_inbox 에 따로 둡니다. 메일은 스레드를 모릅니다 -- 스레드는 상대방 주소로
찾습니다. 스레드가 열리기 전에 보낸 메일은 스레드를 열 때 첫 메시지에 보여줍니다.

함수마다 커넥션을 열고 닫습니다. psycopg 는 동기라 이벤트 루프에서는
asyncio.to_thread 로 부릅니다.
"""

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any

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
    channel_id: str
    sent_by: str | None
    # 부재중 같은 자동응답. 답장할 메일을 고를 때 건너뛴다.
    auto_reply: bool = False
    # 받은 메일을 우리가 받은 시각(맨 위 Received). 없거나 깨졌으면 None.
    received_at: datetime | None = None


# 열 이름은 행 모양에서 딴다. 손으로 나열하면 필드를 추가했을 때 INSERT 가
# 조용히 그 열을 빠뜨린다.
COLUMNS = tuple(field.name for field in fields(OpsMailRow))

# 받은 메일 원본을 맡을 수 있는 횟수와, 맡은 쪽이 끝내기를 기다리는 시간.
# 기다리는 동안 다른 쪽(SNS 재전송, 정리 작업)은 같은 원본을 맡지 못한다.
# inbound.DELIVER_TIMEOUT 은 이 값에서 뺀 값이라 늘 이보다 짧다.
MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=15)

# 맡은 지 이만큼 지나도 끝나지 않은 원본은 횟수가 남아도 포기하고 알린다
# (마지막으로 맡은 쪽이 죽었고, 정리 작업도 그동안 돌지 못한 경우).
ABANDON_AFTER = timedelta(days=1)

INSERT = f"""
INSERT INTO ops_mail_message ({", ".join(COLUMNS)})
VALUES ({", ".join(f"%({name})s" for name in COLUMNS)})
ON CONFLICT (ses_message_id) DO NOTHING
"""

# 끝나지도, 포기를 알리지도 않은 원본. 포기를 알린 뒤에 올리면 "포기" 알림과
# 메일이 엇갈린다. 아래 쿼리들은 표 별칭 i 로 이 조건을 함께 쓴다 -- 쿼리마다 따로
# 적으면 한쪽만 고쳤을 때 정리 작업이 거르는 원본과 다시 맡는 원본이 어긋난다.
_UNFINISHED = "i.processed_at IS NULL AND i.alerted_at IS NULL"

# 다시 맡을 수 있는 원본: 끝나지 않았고 횟수가 남았다.
_RETRYABLE = f"{_UNFINISHED} AND i.attempts < {MAX_ATTEMPTS}"

# 처음이면 넣고, 이미 있으면 다시 맡을 수 있고 다른 쪽이 맡은 지 LEASE 가
# 지났을 때만 다시 맡는다. 맡았으면 몇 번째인지가 돌아온다.
CLAIM = f"""
INSERT INTO ops_mail_inbox AS i (ses_message_id, s3_key, attempts, claimed_at)
VALUES (%s, %s, 1, now())
ON CONFLICT (ses_message_id) DO UPDATE
SET attempts = i.attempts + 1, claimed_at = now()
WHERE {_RETRYABLE} AND i.claimed_at < now() - %s
RETURNING attempts
"""

FINISH = """
UPDATE ops_mail_inbox SET processed_at = now() WHERE ses_message_id = %s
"""

# 끝나지 않았는데 더 다시 하지 않을 원본 중 아직 알리지 않은 것: 횟수를 다 썼고
# 마지막으로 맡은 쪽도 끝내지 못했거나, ABANDON_AFTER 가 지났다.
GIVEN_UP = f"""
SELECT ses_message_id, s3_key, attempts, last_error FROM ops_mail_inbox i
WHERE {_UNFINISHED}
  AND i.claimed_at < now() - %(lease)s
  AND (i.attempts >= {MAX_ATTEMPTS} OR i.claimed_at < now() - %(abandon)s)
"""

MARK_ALERTED = "UPDATE ops_mail_inbox SET alerted_at = now() WHERE ses_message_id = %s"

FAIL = "UPDATE ops_mail_inbox SET last_error = %s WHERE ses_message_id = %s"

# 정리 작업이 더 볼 필요 없는 원본: 다시 맡을 수 없는 것.
SETTLED = f"""
SELECT ses_message_id FROM ops_mail_inbox i
WHERE i.ses_message_id = ANY(%s) AND NOT ({_RETRYABLE})
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

# 상대방 스레드의 상대와, 답장이 걸 받은 메일. 우리가 보낸 메일은 고르지 않는다 --
# SES 가 Message-ID 를 바꿔 써서 걸 수 없고, 채널에서 여럿에게 보낸 새 메일이
# 고르면 답장이 그 메일의 제목을 달고 대화에서 떨어진다. 자동응답도 건너뛴다.
# 받은 메일이 아직 기록되지 않았으면 subject 와 message_id 가 NULL 이다.
REPLY_TARGET = """
SELECT t.counterpart, m.subject, m.message_id, m.references_ids
FROM ops_mail_thread t
LEFT JOIN LATERAL (
  SELECT subject, message_id, references_ids
  FROM ops_mail_message
  WHERE channel_id = t.channel_id AND counterpart = t.counterpart
    AND direction = 'in' AND NOT auto_reply
  ORDER BY COALESCE(received_at, created_at) DESC
  LIMIT 1
) m ON true
WHERE t.channel_id = %s AND t.thread_ts = %s
"""


def settled(ses_message_ids: list[str]) -> set[str]:
    """원본 중 더 처리할 필요가 없는 것을 고릅니다.

    Args:
        ses_message_ids: SES 메시지 ID (객체 키의 끝)

    Returns:
        set[str]: 처리가 끝났거나 횟수를 다 쓴 SES 메시지 ID
    """
    with connect() as conn:
        rows = fetch_all(conn, SETTLED, (ses_message_ids,))
    return {row["ses_message_id"] for row in rows}


def given_up() -> list[dict[str, Any]]:
    """더 다시 처리하지 않는데 아직 알리지 않은 원본을 찾습니다.

    Returns:
        list[dict[str, Any]]: ses_message_id, s3_key, attempts, 마지막으로 실패한
            이유(last_error, 없을 수 있음)
    """
    with connect() as conn:
        return fetch_all(conn, GIVEN_UP, {"lease": LEASE, "abandon": ABANDON_AFTER})


def fail(ses_message_id: str, error: str) -> None:
    """원본 처리가 실패한 이유를 남깁니다.

    Args:
        ses_message_id: SES 메시지 ID
        error: 실패한 이유
    """
    with connect() as conn:
        conn.execute(FAIL, (error, ses_message_id))


def mark_alerted(ses_message_id: str) -> None:
    """포기한 원본을 알렸다고 표시합니다.

    Args:
        ses_message_id: SES 메시지 ID
    """
    with connect() as conn:
        conn.execute(MARK_ALERTED, (ses_message_id,))


def claim(ses_message_id: str, s3_key: str) -> int | None:
    """받은 메일 원본 하나를 이쪽이 처리하겠다고 자리를 잡습니다.

    Args:
        ses_message_id: SES 메시지 ID (객체 키의 끝)
        s3_key: 객체 키

    Returns:
        int | None: 맡았으면 몇 번째 시도인지. 이미 끝났거나, 다른 쪽이 처리 중이거나,
            횟수를 다 썼으면 None
    """
    with connect() as conn:
        rows = fetch_all(conn, CLAIM, (ses_message_id, s3_key, LEASE))
    return rows[0]["attempts"] if rows else None


def finish(row: OpsMailRow) -> None:
    """받은 메일을 남기고 원본 처리를 끝냈다고 표시합니다. 한 트랜잭션입니다.

    Args:
        row: 받은 메일 행
    """
    with connect() as conn:
        conn.execute(INSERT, asdict(row))
        conn.execute(FINISH, (row.ses_message_id,))


def find_thread(channel_id: str, counterpart: str) -> str | None:
    """상대방 스레드를 찾습니다.

    Args:
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        str | None: 스레드 ts. 아직 스레드가 없는 상대면 None
    """
    with connect() as conn:
        rows = fetch_all(conn, FIND_THREAD, (channel_id, counterpart))
    return rows[0]["thread_ts"] if rows else None


def claim_thread(channel_id: str, counterpart: str, thread_ts: str) -> str:
    """새로 연 스레드를 상대방 스레드로 등록합니다.

    Args:
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)
        thread_ts: 새로 연 스레드 ts

    Returns:
        str: 상대방 스레드 ts. 다른 쪽이 먼저 열었으면 그 스레드입니다.
    """
    with connect() as conn:
        conn.execute(CLAIM_THREAD, (channel_id, counterpart, thread_ts))
        rows = fetch_all(conn, FIND_THREAD, (channel_id, counterpart))
    return rows[0]["thread_ts"]


def sent_to(channel_id: str, counterpart: str) -> list[dict[str, Any]]:
    """이 상대에게 보낸 메일을 찾습니다.

    Args:
        channel_id: 메일 채널 ID
        counterpart: 상대방 주소 (소문자)

    Returns:
        list[dict[str, Any]]: 제목·보낸 사람·보낸 시각
    """
    with connect() as conn:
        return fetch_all(conn, SENT_TO, (channel_id, counterpart))


def reply_target(channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    """상대방 스레드라면 그 상대와, 답장이 걸 받은 메일을 찾습니다.

    Args:
        channel_id: 메일 채널 ID
        thread_ts: 스레드 ts

    Returns:
        dict[str, Any] | None: counterpart, subject, message_id, references_ids.
            상대방 스레드가 아니면 None. 받은 메일이 아직 기록되지 않았으면
            counterpart 말고는 None
    """
    with connect() as conn:
        rows = fetch_all(conn, REPLY_TARGET, (channel_id, thread_ts))
    return rows[0] if rows else None


def insert(row: OpsMailRow) -> None:
    """메일 한 통을 남깁니다.

    Args:
        row: 남길 행
    """
    with connect() as conn:
        conn.execute(INSERT, asdict(row))
