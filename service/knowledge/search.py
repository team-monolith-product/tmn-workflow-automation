"""
지식베이스 어휘 검색의 Service Layer입니다.

봇 도구와 MCP 서버가 같은 함수를 부릅니다. 검색 규칙이 둘로 갈리면 한쪽에서
잡히는 것이 다른 쪽에서 안 잡히고, 그 차이를 query_log로는 설명할 수 없게
됩니다.

pg_bigm은 LIKE만 인덱스로 처리하고 ILIKE는 순차 스캔으로 떨어집니다. 그래서
소문자로 접은 표현식에 인덱스를 두고(002) 질의도 접어서 넣습니다.

정제문은 아직 검색하지 않습니다. distilled_text가 전부 비어 있어서, 지금
넣으면 항상 0건인 가지를 유지하게 됩니다.
"""

import json
import time
from typing import Any

import psycopg

from service.knowledge.db import fetch_all

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# 스레드 원문은 평균 1,148자다. 스무 건을 통째로 돌려주면 도구 결과가 2만 자를
# 넘어 에이전트 문맥을 잡아먹는다. 맞은 자리 주변만 보여주고 나머지는 url로
# 넘긴다.
SNIPPET_RADIUS = 120

SEARCH_ITEMS = """
SELECT item.id,
       item.title,
       item.url,
       item.author,
       item.source_created_at,
       item.raw_text,
       data_source.name AS channel
FROM item
JOIN data_source ON data_source.id = item.data_source_id
WHERE lower(item.raw_text) LIKE %(pattern)s
  AND (%(channel)s::text IS NULL OR data_source.name = %(channel)s)
ORDER BY item.source_created_at DESC
LIMIT %(limit)s
"""

LOG_QUERY = """
INSERT INTO query_log (actor, tool, query, filters, result_ids, latency_ms)
VALUES (%(actor)s, %(tool)s, %(query)s, %(filters)s, %(result_ids)s, %(latency_ms)s)
"""


def to_like_pattern(query: str) -> str:
    """질의를 소문자 LIKE 패턴으로 바꿉니다.

    와일드카드를 이스케이프합니다. 그러지 않으면 "50%" 같은 질의가 "50"으로
    시작하는 모든 문서를 긁고, "sg_09dd"의 밑줄이 아무 글자에나 맞습니다.

    Args:
        query: 사용자가 친 검색어

    Returns:
        str: 앞뒤에 %를 붙인 LIKE 패턴
    """
    escaped = (
        query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def build_snippet(raw_text: str, query: str) -> str:
    """검색어가 맞은 자리 주변만 잘라냅니다.

    Args:
        raw_text: 스레드 원문
        query: 사용자가 친 검색어

    Returns:
        str: 잘라낸 구간. 앞뒤가 잘리면 줄임표를 붙인다
    """
    position = raw_text.lower().find(query.lower())
    if position < 0:
        # LIKE는 맞았는데 파이썬에서 못 찾는 경우다. 질의에 와일드카드
        # 문자가 들어 있어 이스케이프한 형태로만 맞은 것이다.
        position = 0

    start = max(0, position - SNIPPET_RADIUS)
    end = min(len(raw_text), position + len(query) + SNIPPET_RADIUS)
    snippet = raw_text[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(raw_text) else "")


def search_items(
    conn: psycopg.Connection,
    query: str,
    actor: str,
    tool: str,
    channel: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """공개 채널 스레드를 어휘 검색합니다.

    질의를 query_log에 남기는 것이 이 함수의 책임입니다. 호출부에 맡기면
    빠뜨린 경로가 생기고, 무엇이 검색되지 않는지는 이 표로만 알 수 있습니다.

    Args:
        conn: 커넥션
        query: 검색어
        actor: 질의한 사람의 이메일
        tool: 질의가 들어온 경로. "slack" 또는 "mcp"
        channel: 이 채널로 좁힙니다. None이면 전체
        limit: 최대 결과 수

    Returns:
        list[dict[str, Any]]: 최신순 결과. 원문 대신 snippet을 담는다
    """
    parameters = {
        "pattern": to_like_pattern(query),
        "channel": channel,
        "limit": min(limit, MAX_LIMIT),
    }

    started = time.monotonic()
    rows = fetch_all(conn, SEARCH_ITEMS, parameters)
    latency_ms = int((time.monotonic() - started) * 1000)

    with conn.cursor() as cur:
        cur.execute(
            LOG_QUERY,
            {
                "actor": actor,
                "tool": tool,
                "query": query,
                "filters": json.dumps({"channel": channel, "limit": limit}),
                "result_ids": [row["id"] for row in rows],
                "latency_ms": latency_ms,
            },
        )

    return [
        {
            "title": row["title"],
            "url": row["url"],
            "author": row["author"],
            "channel": row["channel"],
            "created_at": row["source_created_at"].date().isoformat(),
            "snippet": build_snippet(row["raw_text"], query),
        }
        for row in rows
    ]
