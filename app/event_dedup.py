"""
Slack 이벤트 중복 처리 방지 모듈

Socket Mode에서 이벤트 핸들러 완료 후에야 envelope ack가 전송되므로,
LLM 호출 등 오래 걸리는 핸들러는 Slack이 이벤트를 재전송하게 됩니다.
이 모듈은 event_id 기반 TTL 캐시로 중복 이벤트를 필터링합니다.
"""

from cachetools import TTLCache

# 5분 TTL, 최대 1000개 이벤트 추적
_processed_events: TTLCache = TTLCache(maxsize=1000, ttl=300)


def is_duplicate_event(body: dict) -> bool:
    """이미 처리 중이거나 처리된 이벤트인지 확인합니다.

    Args:
        body: Slack 이벤트 body (event_id 포함)

    Returns:
        True이면 중복 이벤트이므로 스킵해야 합니다.
    """
    event_id = body.get("event_id")
    if not event_id:
        return False

    if event_id in _processed_events:
        return True

    _processed_events[event_id] = True
    return False
