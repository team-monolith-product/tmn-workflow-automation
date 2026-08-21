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
    channel_id, thread_ts, content, message_type, message_key, approved_by
) VALUES (
    %(channel_id)s, %(thread_ts)s, %(content)s,
    %(message_type)s, %(message_key)s, %(approved_by)s
) RETURNING id
"""

INSERT_RECIPIENT = """
INSERT INTO sms_recipient (send_id, phone, name, change_word)
VALUES (%(send_id)s, %(phone)s, %(name)s, %(change_word)s)
"""


def record(
    *,
    channel_id: str,
    thread_ts: str,
    content: str,
    message_type: str,
    message_key: str | None,
    approved_by: str,
    targets: list[dict[str, Any]],
) -> None:
    """발송 한 건과 수신자를 남깁니다.

    Args:
        channel_id: 발송을 승인한 채널
        thread_ts: 카드가 올라간 스레드. 같은 스레드의 발송이 한 캠페인이다
        content: 벤더로 나간 문안. send() 가 돌려준 값을 그대로 넘긴다
        message_type: SMS 또는 LMS
        message_key: 뿌리오 접수 키. 벤더가 빠뜨리면 없을 수 있다
        approved_by: [보내기] 를 누른 사람의 Slack 사용자 ID
        targets: 벤더로 나간 targets 배열
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            INSERT_SEND,
            {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "content": content,
                "message_type": message_type,
                "message_key": message_key,
                "approved_by": approved_by,
            },
        )
        send_id = cur.fetchone()["id"]
        cur.executemany(
            INSERT_RECIPIENT,
            [
                {
                    "send_id": send_id,
                    "phone": target["to"],
                    "name": target.get("name"),
                    "change_word": (
                        json.dumps(target["changeWord"], ensure_ascii=False)
                        if "changeWord" in target
                        else None
                    ),
                }
                for target in targets
            ],
        )
