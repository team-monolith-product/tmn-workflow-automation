"""
Slack 스레드를 지식베이스 item 행으로 정규화하는 Service Layer입니다.

Socket Mode 이벤트와 워크스페이스 Export 백필이 같은 함수를 거칩니다.
그래서 입력은 Slack API의 conversations.replies 응답 형태(메시지 목록)로 통일합니다.

메시지 한 건이 아니라 스레드 전체를 한 행으로 씁니다. "그거 해결됐어요" 같은
메시지는 앞뒤 맥락 없이는 검색해도 쓸모가 없기 때문입니다. 답글이 하나 달리면
부모와 형제를 다시 가져와 행을 통째로 덮어씁니다.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

UPSERT_ITEM = """
INSERT INTO item (
    data_source_id, external_id,
    url, title, author,
    source_created_at, source_updated_at,
    raw, raw_text, metadata, content_hash,
    distill_state, distill_after
)
VALUES (
    %(data_source_id)s, %(external_id)s,
    %(url)s, %(title)s, %(author)s,
    %(source_created_at)s, %(source_updated_at)s,
    %(raw)s, %(raw_text)s, %(metadata)s, %(content_hash)s,
    'pending', %(distill_after)s
)
ON CONFLICT (data_source_id, external_id) DO UPDATE SET
    url               = EXCLUDED.url,
    title             = EXCLUDED.title,
    source_updated_at = EXCLUDED.source_updated_at,
    raw               = EXCLUDED.raw,
    raw_text          = EXCLUDED.raw_text,
    metadata          = EXCLUDED.metadata,
    content_hash      = EXCLUDED.content_hash,
    -- 내용이 그대로면 정제 상태를 건드리지 않는다. 이벤트 재전송이나 Export
    -- 재적재로 이미 끝난 정제를 다시 돌리지 않기 위해서다.
    distill_state     = CASE
                            WHEN item.content_hash = EXCLUDED.content_hash
                            THEN item.distill_state
                            ELSE 'pending'
                        END,
    distill_after     = CASE
                            WHEN item.content_hash = EXCLUDED.content_hash
                            THEN item.distill_after
                            ELSE EXCLUDED.distill_after
                        END,
    indexed_at        = now()
RETURNING id, (xmax = 0) AS inserted
"""


def _ts_to_datetime(ts: str) -> datetime:
    """Slack ts 문자열을 UTC datetime으로 변환합니다.

    Args:
        ts: "1785338608.347569" 형태의 Slack 타임스탬프

    Returns:
        datetime: UTC 시각
    """
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _count_reactions(messages: list[dict[str, Any]]) -> int:
    """스레드 전체의 리액션 수를 합산합니다.

    Args:
        messages: 스레드 메시지 목록

    Returns:
        int: 리액션 총 개수
    """
    return sum(
        reaction.get("count", 0)
        for message in messages
        for reaction in message.get("reactions", [])
    )


def build_raw_text(messages: list[dict[str, Any]]) -> str:
    """bigram 인덱스가 검색할 평문을 만듭니다.

    임베딩 입력이 아니라 키워드 검색 대상이므로 원문을 최대한 보존합니다.
    작성자를 함께 남기는 이유는 "누가 말했나"도 검색어가 되기 때문입니다.

    Args:
        messages: 스레드 메시지 목록

    Returns:
        str: 줄바꿈으로 이어붙인 평문
    """
    return "\n".join(
        f"{message.get('user') or message.get('bot_id') or ''}: {message.get('text', '')}"
        for message in messages
    )


def compute_content_hash(raw_text: str) -> str:
    """내용 변경 판정에 쓸 해시를 계산합니다.

    Socket Mode 이벤트는 재전송될 수 있고 Export 백필은 이미 적재한 스레드를
    다시 훑습니다. 내용이 같으면 정제를 다시 돌리지 않으려고 씁니다.

    Args:
        raw_text: build_raw_text 결과

    Returns:
        str: sha256 hex digest
    """
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def build_thread_row(
    data_source_id: int,
    channel_id: str,
    messages: list[dict[str, Any]],
    workspace_domain: str,
    distill_delay_seconds: int,
) -> dict[str, Any]:
    """스레드 메시지 목록을 item 행으로 정규화합니다.

    Args:
        data_source_id: 채널에 대응하는 data_source.id
        channel_id: Slack 채널 ID
        messages: conversations.replies 순서 그대로의 메시지 목록. 첫 항목이 부모
        workspace_domain: permalink 구성용 워크스페이스 도메인
        distill_delay_seconds: 마지막 활동 이후 정제를 미룰 시간

    Returns:
        dict[str, Any]: UPSERT_ITEM 바인딩 파라미터
    """
    parent = messages[0]
    thread_ts = parent["ts"]
    last_ts = messages[-1]["ts"]

    raw_text = build_raw_text(messages)
    participants = sorted(
        {message["user"] for message in messages if message.get("user")}
    )
    last_activity = _ts_to_datetime(last_ts)

    return {
        "data_source_id": data_source_id,
        "external_id": f"{channel_id}:{thread_ts}",
        "url": (
            f"https://{workspace_domain}/archives/{channel_id}/p{thread_ts.replace('.', '')}"
        ),
        # 스레드에 제목이 없으므로 부모 메시지 첫 줄을 쓴다.
        "title": parent.get("text", "").split("\n")[0][:200],
        "author": parent.get("user") or parent.get("bot_id"),
        "source_created_at": _ts_to_datetime(thread_ts),
        "source_updated_at": last_activity,
        "raw": json.dumps(messages, ensure_ascii=False),
        "raw_text": raw_text,
        # 소스별 필드. index_score의 입력이고 검색 경로는 산출된 점수만 본다.
        "metadata": json.dumps(
            {
                "channel_id": channel_id,
                "participants": participants,
                "reply_count": len(messages) - 1,
                "reaction_count": _count_reactions(messages),
            },
            ensure_ascii=False,
        ),
        "content_hash": compute_content_hash(raw_text),
        "distill_after": last_activity + timedelta(seconds=distill_delay_seconds),
    }


def upsert_thread(conn: psycopg.Connection, row: dict[str, Any]) -> dict[str, Any]:
    """스레드 행을 적재하거나 갱신합니다.

    Args:
        conn: 커넥션
        row: build_thread_row 결과

    Returns:
        dict[str, Any]: id와 inserted 여부
    """
    with conn.cursor() as cur:
        cur.execute(UPSERT_ITEM, row)
        return cur.fetchone()
