"""
발송 이력을 남깁니다.

읽는 쪽은 만들지 않습니다. 에이전트가 이미 query_knowledge 로 임의 SQL 을
돌리므로, 같은 DB 에 넣어두면 "이 번호로 뭘 보냈나" 를 물어보면 답합니다.
"""

import json
from datetime import datetime
from typing import Any

from service.db import connect
from service.sms.send import KST, SEND_TIME_FORMAT

# 채널이 어느 사업인지. 매핑 표를 따로 두면 조인이 생기고, 조인은 에이전트가
# SQL 을 짤 때 틀릴 자리가 된다. 십여 개짜리 상수라 여기 둔다.
# 매핑이 늘면 UPDATE sms_log SET project=... WHERE channel_id=... 로 소급한다.
PROJECT = {
    "C0AP8CG1Y6N": "26기업연계정보교원연수",
    "C0BRF9XJ40N": "26기업연계정보교원연수",
}

INSERT = """
INSERT INTO sms_log (
    ref_key, message_key, channel_id, project, thread_ts, sender,
    content, message_type, approved_by, scheduled_at,
    phone, name, change_word
) VALUES (
    %(ref_key)s, %(message_key)s, %(channel_id)s, %(project)s, %(thread_ts)s,
    %(sender)s, %(content)s, %(message_type)s, %(approved_by)s, %(scheduled_at)s,
    %(phone)s, %(name)s, %(change_word)s
)
"""


def record(
    *,
    ref_key: str,
    channel_id: str,
    thread_ts: str,
    sender: str,
    content: str,
    message_type: str,
    message_key: str | None,
    send_time: str | None,
    approved_by: str,
    targets: list[dict[str, Any]],
) -> None:
    """발송 한 건을 받는 사람마다 한 행으로 남깁니다.

    Args:
        ref_key: 발송을 가리키는 우리 쪽 키. 같은 값이 한 번의 발송이다
        channel_id: 발송을 승인한 채널
        thread_ts: 카드가 올라간 스레드
        sender: 발신번호
        content: 벤더로 나간 문안. send() 가 돌려준 값을 그대로 넘긴다
        message_type: SMS 또는 LMS
        message_key: 뿌리오 접수 키. 벤더가 빠뜨리면 없을 수 있다
        send_time: 예약 시각(벤더 형식 KST). 즉시 발송이면 없다
        approved_by: [보내기] 를 누른 사람의 Slack 사용자 ID
        targets: 벤더로 나간 targets 배열
    """
    shared = {
        "ref_key": ref_key,
        "message_key": message_key,
        "channel_id": channel_id,
        "project": PROJECT.get(channel_id),
        "thread_ts": thread_ts,
        "sender": sender,
        "content": content,
        "message_type": message_type,
        "approved_by": approved_by,
        "scheduled_at": (
            datetime.strptime(send_time, SEND_TIME_FORMAT).replace(tzinfo=KST)
            if send_time
            else None
        ),
    }
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            INSERT,
            [
                {
                    **shared,
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
