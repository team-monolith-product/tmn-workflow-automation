"""
facts 를 만들어 잠시 들고 있는다.

MCP 도구·HTTP 엔드포인트·스케줄 작업이 전부 이 하나를 부른다. 매번 시트를
읽으면 질문 하나에 구글 호출이 2회씩 들고, 같은 질문을 두 도구가 이어서
부르면 사이에 시트가 바뀌어 숫자가 어긋날 수 있다. 그래서 잠깐 붙든다.

DB 에 적재하지 않는다. 지식베이스 DB 는 query_knowledge 가 임의 SQL 로 읽는
곳이라 거기 넣는 순간 이 도구와 무관하게 SQL 로도 나간다. 스냅샷 이력이
필요하면 매출장 자체의 버전 기록이 있다.

프로세스마다 캐시가 따로다(슬랙 봇·FastAPI). 같은 순간 두 프로세스가 다른
숫자를 보일 수 있는 폭은 TTL 이내이고, 그 사이 바뀐 것은 시트다.
"""

import threading
import time
from typing import Any

from service.revenue.build import build_facts
from service.revenue.config import load_enrichment, load_sources, load_taxonomy
from service.revenue.ledger import fetch_ledger

# 매출장은 경리가 하루 몇 번 고치는 시트다. 이 안의 지연은 감수한다.
CACHE_TTL_SECONDS = 600

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0


def build_fresh() -> dict[str, Any]:
    """시트를 읽어 facts 를 새로 만든다. 캐시를 거치지 않는다.

    스케줄 작업이 쓴다. 검증 경고를 슬랙에 보내는 쪽이 10분 묵은 것을 보면 안 된다.
    """
    sources = load_sources()
    raw = fetch_ledger(sources)
    return build_facts(raw, sources, load_taxonomy(), load_enrichment(sources))


def get_facts(max_age_seconds: int = CACHE_TTL_SECONDS) -> dict[str, Any]:
    """facts 를 돌려준다. max_age_seconds 안이면 캐시, 아니면 새로 만든다.

    잠금은 빌드 전체를 감싼다. 캐시가 비었을 때 여럿이 동시에 들어오면 전부
    시트를 읽으러 가고, 쿼터는 계정 단위라 그만큼 같이 줄어든다.

    Args:
        max_age_seconds: 허용하는 캐시 나이. 0 이면 항상 새로 만든다

    Returns:
        dict: facts
    """
    global _cached, _cached_at
    with _lock:
        if _cached is not None and time.monotonic() - _cached_at < max_age_seconds:
            return _cached
        _cached = build_fresh()
        _cached_at = time.monotonic()
        return _cached


def clear_cache() -> None:
    """테스트용."""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0
