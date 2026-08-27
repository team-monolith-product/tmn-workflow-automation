"""
발송 이력을 남깁니다.

읽는 쪽은 만들지 않습니다. 에이전트가 이미 query_knowledge 로 임의 SQL 을
돌리므로, 같은 DB 에 넣어두면 "이 번호로 뭘 보냈나" 를 물어보면 답합니다.
"""

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime

from service.config import load_config
from service.db import connect
from service.sms.send import Sent


@dataclass(frozen=True)
class SmsLogRow:
    """sms_log 한 행. 받는 사람 하나가 한 행입니다."""

    ref_key: str
    message_key: str | None
    channel_id: str
    project: str | None
    thread_ts: str
    sender: str
    content: str
    message_type: str
    approved_by: str
    scheduled_at: datetime | None
    phone: str
    name: str | None
    change_word: str | None


# 열 이름은 행 모양에서 딴다. 손으로 나열하면 필드를 추가했을 때 psycopg 가
# 남는 키를 무시해 INSERT 가 조용히 그 열을 빠뜨린다.
SMS_LOG_COLUMNS = tuple(field.name for field in fields(SmsLogRow))

INSERT = f"""
INSERT INTO sms_log ({", ".join(SMS_LOG_COLUMNS)})
VALUES ({", ".join(f"%({name})s" for name in SMS_LOG_COLUMNS)})
"""


def _change_word(target: dict) -> str | None:
    """치환값을 jsonb 에 넣을 문자열로. 치환이 없으면 남기지 않습니다."""
    if "changeWord" not in target:
        return None
    return json.dumps(target["changeWord"], ensure_ascii=False)


def build_rows(
    sent: Sent, *, channel_id: str, thread_ts: str, approved_by: str
) -> list[SmsLogRow]:
    """발송 한 건을 받는 사람 수만큼의 행으로 폅니다.

    Args:
        sent: 벤더로 실제 나간 값
        channel_id: 발송을 승인한 채널
        thread_ts: 카드가 올라간 스레드
        approved_by: [보내기] 를 누른 사람의 Slack 사용자 ID

    Returns:
        list[SmsLogRow]: 수신자 수만큼의 행
    """
    project = load_config().sms_projects.get(channel_id)
    return [
        SmsLogRow(
            ref_key=sent.ref_key,
            message_key=sent.message_key,
            channel_id=channel_id,
            project=project,
            thread_ts=thread_ts,
            sender=sent.sender,
            content=sent.content,
            message_type=sent.message_type,
            approved_by=approved_by,
            scheduled_at=sent.send_at,
            phone=target["to"],
            name=target.get("name"),
            change_word=_change_word(target),
        )
        for target in sent.targets
    ]


def record(sent: Sent, *, channel_id: str, thread_ts: str, approved_by: str) -> None:
    """발송 한 건을 받는 사람마다 한 행으로 남깁니다.

    psycopg 는 동기라 부르는 쪽에서 스레드로 넘깁니다.

    Args:
        sent: 벤더로 실제 나간 값
        channel_id: 발송을 승인한 채널
        thread_ts: 카드가 올라간 스레드
        approved_by: [보내기] 를 누른 사람의 Slack 사용자 ID

    Returns:
        None
    """
    rows = build_rows(
        sent, channel_id=channel_id, thread_ts=thread_ts, approved_by=approved_by
    )
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(INSERT, [asdict(row) for row in rows])
