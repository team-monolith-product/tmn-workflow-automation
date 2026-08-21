"""
발송 이력을 남깁니다.

읽는 쪽은 만들지 않습니다. 에이전트가 이미 query_knowledge 로 임의 SQL 을
돌리므로, 같은 DB 에 넣어두면 "이 번호로 뭘 보냈나" 를 물어보면 답합니다.
"""

import json
from typing import Any

from service.knowledge.db import connect

INSERT_SEND = """
INSERT INTO sms_send (
    channel_id, thread_ts, root_ts, content, message_type, message_key, approved_by
) VALUES (
    %(channel_id)s, %(thread_ts)s, %(root_ts)s, %(content)s,
    %(message_type)s, %(message_key)s, %(approved_by)s
) RETURNING id
"""

INSERT_RECIPIENT = """
INSERT INTO sms_recipient (send_id, phone, name, change_word)
VALUES (%(send_id)s, %(phone)s, %(name)s, %(change_word)s)
"""

FIND_ROOT = """
SELECT root_ts FROM sms_send
WHERE channel_id = %(channel_id)s AND thread_ts = %(thread_ts)s
ORDER BY sent_at LIMIT 1
"""


def record(
    *,
    channel_id: str,
    thread_ts: str,
    content: str,
    message_type: str,
    message_key: str,
    approved_by: str,
    targets: list[dict[str, Any]],
) -> int:
    """발송 한 건과 수신자를 남깁니다.

    한 트랜잭션입니다. 수신자를 못 넣었는데 발송 행만 남으면, 아무에게도 안
    보낸 발송이 이력에 있는 것처럼 보입니다.

    Args:
        channel_id: 발송을 승인한 채널
        thread_ts: 카드가 올라간 스레드
        content: 벤더로 나간 치환 전 원문
        message_type: SMS 또는 LMS
        message_key: 뿌리오 접수 키
        approved_by: [보내기] 를 누른 사람의 Slack 사용자 ID
        targets: 벤더로 나간 targets 배열

    Returns:
        int: sms_send.id
    """
    with connect() as conn:
        with conn.cursor() as cur:
            # 같은 스레드에서 다시 보내면 첫 발송이 캠페인 뿌리다. 없으면
            # 이번이 첫 발송이라 자기 자신을 가리킨다.
            cur.execute(FIND_ROOT, {"channel_id": channel_id, "thread_ts": thread_ts})
            found = cur.fetchone()

            cur.execute(
                INSERT_SEND,
                {
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "root_ts": found["root_ts"] if found else thread_ts,
                    "content": content,
                    "message_type": message_type,
                    "message_key": message_key,
                    "approved_by": approved_by,
                },
            )
            send_id = cur.fetchone()["id"]

            for target in targets:
                change_word = target.get("changeWord")
                cur.execute(
                    INSERT_RECIPIENT,
                    {
                        "send_id": send_id,
                        "phone": target["to"],
                        "name": target.get("name"),
                        "change_word": (
                            json.dumps(change_word, ensure_ascii=False)
                            if change_word
                            else None
                        ),
                    },
                )
    return send_id
