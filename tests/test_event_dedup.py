"""
이벤트 중복 처리 방지 모듈 테스트
"""

from app.event_dedup import is_duplicate_event, _processed_events


def setup_function():
    """각 테스트 전에 캐시를 초기화합니다."""
    _processed_events.clear()


def test_first_event_is_not_duplicate():
    """최초 이벤트는 중복이 아닙니다."""
    body = {"event_id": "Ev001", "event": {"type": "app_mention"}}
    assert is_duplicate_event(body) is False


def test_same_event_id_is_duplicate():
    """동일한 event_id로 두 번째 호출하면 중복입니다."""
    body = {"event_id": "Ev001", "event": {"type": "app_mention"}}
    assert is_duplicate_event(body) is False
    assert is_duplicate_event(body) is True


def test_different_event_ids_are_not_duplicates():
    """서로 다른 event_id는 중복이 아닙니다."""
    body1 = {"event_id": "Ev001", "event": {"type": "app_mention"}}
    body2 = {"event_id": "Ev002", "event": {"type": "app_mention"}}
    assert is_duplicate_event(body1) is False
    assert is_duplicate_event(body2) is False


def test_missing_event_id_is_not_duplicate():
    """event_id가 없는 body는 중복으로 처리하지 않습니다."""
    body = {"event": {"type": "app_mention"}}
    assert is_duplicate_event(body) is False
    assert is_duplicate_event(body) is False


def test_triple_retry_only_first_passes():
    """Socket Mode 재시도 시나리오: 동일 이벤트 3번 전달 시 첫 번째만 통과합니다."""
    body = {"event_id": "Ev_retry_test", "event": {"type": "app_mention"}}
    results = [is_duplicate_event(body) for _ in range(3)]
    assert results == [False, True, True]
