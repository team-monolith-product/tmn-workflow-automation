"""
발송 기록입니다. 한 사람에게 한 번 보낸 것이 sms_send 한 행입니다.

중복 차단은 DB 가 합니다. INSERT ... ON CONFLICT DO NOTHING 이 원자적이라
동시에 두 실행이 같은 사람을 넣으려 하면 한쪽만 행을 받습니다.

상태는 시각으로 남깁니다. status 한 컬럼이면 도달 확인이 붙을 때 '발송'을
'도달'로 덮어써야 하고 언제 보냈는지가 사라집니다.

    sent_at·failed_at 둘 다 NULL   접수 여부를 모른다. 재시도가 막힌다
    sent_at                        벤더가 접수했다
    failed_at                      벤더가 거절했다. 재시도가 열린다
    confirmed_at                   도달을 확인했다

campaign 이 NULL 이면 개인 CS 라 중복 차단을 받지 않습니다.
"""

import json
import re
from typing import Any

from service.knowledge.db import connect

CLAIM = """
INSERT INTO sms_send
    (campaign, phone, content, variables, channel_id, requested_by)
SELECT %(campaign)s, t.phone, %(content)s, t.variables,
       %(channel_id)s, %(requested_by)s
FROM unnest(%(phones)s::text[], %(variables)s::jsonb[]) AS t(phone, variables)
ON CONFLICT DO NOTHING
RETURNING id, phone
"""

SENT = """
UPDATE sms_send
SET sent_at = now(), scheduled_for = %(scheduled_for)s, message_key = %(message_key)s
WHERE id = ANY(%(ids)s)
"""

FAILED = "UPDATE sms_send SET failed_at = now() WHERE id = ANY(%(ids)s)"

HISTORY = """
SELECT campaign, content, variables, claimed_at, sent_at, scheduled_for,
       failed_at, confirmed_at, requested_by
FROM sms_send
WHERE phone = %(phone)s
ORDER BY claimed_at DESC
LIMIT %(limit)s
"""

PENDING = """
SELECT phone, claimed_at
FROM sms_send
WHERE campaign = %(campaign)s AND sent_at IS NULL AND failed_at IS NULL
ORDER BY claimed_at
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
    entries: list[dict[str, Any]],
    *,
    content: str,
    channel_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, int]:
    """보낼 사람의 자리를 잡습니다. 이미 잡힌 사람은 안 돌려줍니다.

    벤더를 부르기 전에 넣습니다. 넣지 않고 보내면 그 사이 다른 실행이 같은
    사람을 대상으로 보고 또 보냅니다.

    치환값을 함께 남깁니다. 원문만 남기면 나중에 "이 사람이 받은 문자"를
    되살릴 때 [*이름*] 자리가 빈 채로 보입니다.

    Args:
        campaign: 발송 건 식별자. None 이면 개인 CS 라 중복 차단을 안 받는다
        entries: to·name·var1~var8 을 담은 수신자 목록
        content: 치환 전 원문
        channel_id: 어느 채널에서 시켰나
        requested_by: 누가 시켰나

    Returns:
        dict[str, int]: 자리를 잡은 {번호: 행 id}. 이미 보낸 사람은 빠진다
    """
    phones, variables = [], []
    for entry in entries:
        phones.append(digits(entry["to"]))
        values = {key: value for key, value in entry.items() if key != "to" and value}
        variables.append(json.dumps(values, ensure_ascii=False))

    with connect() as conn:
        rows = conn.execute(
            CLAIM,
            {
                "campaign": campaign,
                "phones": phones,
                "variables": variables,
                "content": content,
                "channel_id": channel_id,
                "requested_by": requested_by,
            },
        ).fetchall()
    return {row["phone"]: row["id"] for row in rows}


def mark_sent(
    ids: list[int], *, message_key: str | None = None, scheduled_for: Any = None
) -> None:
    """벤더가 접수한 것으로 표시합니다.

    Args:
        ids: claim 이 돌려준 행 id
        message_key: 벤더 접수번호
        scheduled_for: 예약이면 나갈 시각. 즉시 발송이면 None
    """
    if not ids:
        return
    with connect() as conn:
        conn.execute(
            SENT,
            {"ids": ids, "message_key": message_key, "scheduled_for": scheduled_for},
        )


def mark_failed(ids: list[int]) -> None:
    """벤더가 거절한 것으로 표시합니다. 재시도가 열립니다.

    Args:
        ids: claim 이 돌려준 행 id
    """
    if not ids:
        return
    with connect() as conn:
        conn.execute(FAILED, {"ids": ids})


def history(phone: str, limit: int = 20) -> list[dict[str, Any]]:
    """그 번호에게 보낸 것을 최근 순으로 돌려줍니다.

    "저 못 받았는데요" 에 답하는 경로입니다.

    Args:
        phone: 번호 (표기 무관)
        limit: 최대 건수

    Returns:
        list[dict[str, Any]]: campaign·content·variables 와 각 단계의 시각
    """
    with connect(read_only=True) as conn:
        return conn.execute(
            HISTORY, {"phone": digits(phone), "limit": limit}
        ).fetchall()


def pending(campaign: str) -> list[dict[str, Any]]:
    """그 캠페인에서 접수 여부를 모르는 채 막혀 있는 사람을 돌려줍니다.

    타임아웃·5xx 로 굳은 건들입니다. 뿌리오 웹에서 확인한 뒤 사람이 풀어야
    재시도가 열립니다.

    Args:
        campaign: 발송 건 식별자

    Returns:
        list[dict[str, Any]]: phone·claimed_at
    """
    with connect(read_only=True) as conn:
        return conn.execute(PENDING, {"campaign": campaign}).fetchall()
