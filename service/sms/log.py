"""
발송 기록입니다. 한 사람에게 한 번 보낸 것이 sms_send 한 행입니다.

중복 차단은 DB 가 합니다. INSERT ... ON CONFLICT DO NOTHING 이 원자적이라
동시에 두 실행이 같은 사람을 넣으려 하면 한쪽만 행을 받습니다. 시트로 하던
때처럼 "읽고 → 비었나 보고 → 쓴다" 사이가 열려 있지 않습니다.

상태는 셋입니다.

    발송중  접수 여부를 모른다(타임아웃·5xx). 재시도가 막힌다
    발송    벤더가 접수했다
    실패    벤더가 거절한 것이 확실하다. 재시도가 열린다

campaign 이 NULL 이면 개인 CS 라 중복 차단을 받지 않습니다 — 같은 사람에게
여러 번 보내는 것이 정상이기 때문입니다.
"""

import re
from typing import Any

from service.knowledge.db import connect

CLAIM = """
INSERT INTO sms_send (campaign, phone, status, content, channel_id, requested_by)
SELECT %(campaign)s, phone, '발송중', %(content)s, %(channel_id)s, %(requested_by)s
FROM unnest(%(phones)s::text[]) AS phone
ON CONFLICT DO NOTHING
RETURNING id, phone
"""

MARK = """
UPDATE sms_send
SET status = %(status)s,
    message_key = COALESCE(%(message_key)s, message_key),
    sent_at = %(sent_at)s
WHERE id = ANY(%(ids)s)
"""

HISTORY = """
SELECT campaign, status, content, sent_at, created_at, requested_by
FROM sms_send
WHERE phone = %(phone)s
ORDER BY created_at DESC
LIMIT %(limit)s
"""

PENDING = """
SELECT phone, created_at
FROM sms_send
WHERE campaign = %(campaign)s AND status = '발송중'
ORDER BY created_at
"""


def digits(phone: str) -> str:
    """대조에 쓸 번호를 만듭니다. 숫자만 남깁니다.

    Args:
        phone: 번호 (표기 무관)

    Returns:
        str: 숫자만 남은 번호
    """
    return re.sub(r"\D", "", phone)


def claim(
    campaign: str | None,
    phones: list[str],
    *,
    content: str,
    channel_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, int]:
    """보낼 사람의 자리를 잡습니다. 이미 잡힌 사람은 안 돌려줍니다.

    벤더를 부르기 전에 '발송중'으로 넣습니다. 넣지 않고 보내면 그 사이 다른
    실행이 같은 사람을 대상으로 보고 또 보냅니다.

    Args:
        campaign: 발송 건 식별자. None 이면 개인 CS 라 중복 차단을 안 받는다
        phones: 수신번호 (표기 무관)
        content: 치환 전 원문
        channel_id: 어느 채널에서 시켰나
        requested_by: 누가 시켰나

    Returns:
        dict[str, int]: 자리를 잡은 {번호: 행 id}. 이미 보낸 사람은 빠진다
    """
    with connect() as conn:
        rows = conn.execute(
            CLAIM,
            {
                "campaign": campaign,
                "phones": [digits(phone) for phone in phones],
                "content": content,
                "channel_id": channel_id,
                "requested_by": requested_by,
            },
        ).fetchall()
    return {row["phone"]: row["id"] for row in rows}


def mark(
    ids: list[int],
    status: str,
    *,
    message_key: str | None = None,
    sent_at: Any = None,
) -> None:
    """잡아둔 행의 상태를 바꿉니다.

    Args:
        ids: claim 이 돌려준 행 id
        status: 발송 · 실패
        message_key: 벤더 접수번호
        sent_at: 나갈 시각. 예약이면 예약 시각이다
    """
    if not ids:
        return
    with connect() as conn:
        conn.execute(
            MARK,
            {
                "ids": ids,
                "status": status,
                "message_key": message_key,
                "sent_at": sent_at,
            },
        )


def history(phone: str, limit: int = 20) -> list[dict[str, Any]]:
    """그 번호에게 보낸 것을 최근 순으로 돌려줍니다.

    "저 못 받았는데요" 에 답하는 경로입니다.

    Args:
        phone: 번호 (표기 무관)
        limit: 최대 건수

    Returns:
        list[dict[str, Any]]: campaign·status·content·sent_at·requested_by
    """
    with connect(read_only=True) as conn:
        return conn.execute(
            HISTORY, {"phone": digits(phone), "limit": limit}
        ).fetchall()


def pending(campaign: str) -> list[dict[str, Any]]:
    """그 캠페인에서 '발송중'으로 막혀 있는 사람을 돌려줍니다.

    타임아웃으로 굳은 건들입니다. 뿌리오 웹에서 확인한 뒤 사람이 풀어야
    재시도가 열립니다.

    Args:
        campaign: 발송 건 식별자

    Returns:
        list[dict[str, Any]]: phone·created_at
    """
    with connect(read_only=True) as conn:
        return conn.execute(PENDING, {"campaign": campaign}).fetchall()
