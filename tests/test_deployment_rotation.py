"""배포 담당자 로테이션 서비스 테스트"""

from datetime import date
from unittest.mock import patch

from service.deployment_rotation import (
    _get_rotation_offset,
    get_weekday_schedule,
    get_todays_deployer,
    WEEKDAY_NAMES,
)

MEMBERS = ["U_A", "U_B", "U_C"]


def test_weekday_names():
    """한국어 요일 이름 검증"""
    assert WEEKDAY_NAMES == ["월", "화", "수", "목", "금"]


def test_rotation_offset_deterministic():
    """같은 연월이면 항상 같은 오프셋"""
    assert _get_rotation_offset(2026, 4, 3) == _get_rotation_offset(2026, 4, 3)


def test_rotation_offset_changes_monthly():
    """월이 바뀌면 오프셋이 변경될 수 있음"""
    offsets = {_get_rotation_offset(2026, m, 3) for m in range(1, 13)}
    # 3명이면 12개월 중 최소 2가지 이상의 오프셋이 나와야 함
    assert len(offsets) >= 2


def test_get_weekday_schedule_covers_all_weekdays():
    """스케줄이 월~금 5일을 모두 포함"""
    schedule = get_weekday_schedule(2026, 4, MEMBERS)
    assert set(schedule.keys()) == {0, 1, 2, 3, 4}


def test_get_weekday_schedule_uses_all_members():
    """멤버 수가 5 미만이면 일부 멤버가 여러 요일에 배정됨"""
    schedule = get_weekday_schedule(2026, 4, MEMBERS)
    assigned = set(schedule.values())
    # 3명이 5요일을 커버하므로 모든 멤버가 1번 이상 배정
    assert assigned == set(MEMBERS)


def test_get_weekday_schedule_round_robin():
    """라운드 로빈으로 순환 배정 확인"""
    schedule = get_weekday_schedule(2026, 4, MEMBERS)
    offset = _get_rotation_offset(2026, 4, len(MEMBERS))
    for i in range(5):
        expected = MEMBERS[(i + offset) % len(MEMBERS)]
        assert schedule[i] == expected


def test_get_todays_deployer_on_business_day():
    """영업일에는 배포 담당자 반환"""
    # 2026-04-06은 월요일
    with patch("service.deployment_rotation.date") as mock_date, patch(
        "service.deployment_rotation.is_business_day", return_value=True
    ):
        mock_date.today.return_value = date(2026, 4, 6)
        result = get_todays_deployer(MEMBERS)
        assert result is not None
        assert result in MEMBERS


def test_get_todays_deployer_on_non_business_day():
    """비영업일(주말/공휴일)에는 None 반환"""
    with patch("service.deployment_rotation.date") as mock_date, patch(
        "service.deployment_rotation.is_business_day", return_value=False
    ):
        mock_date.today.return_value = date(2026, 4, 5)  # 일요일
        result = get_todays_deployer(MEMBERS)
        assert result is None


def test_schedule_consistency_across_month():
    """같은 월 내에서는 스케줄이 일관됨"""
    s1 = get_weekday_schedule(2026, 4, MEMBERS)
    s2 = get_weekday_schedule(2026, 4, MEMBERS)
    assert s1 == s2


def test_schedule_different_months():
    """다른 월에는 스케줄이 다를 수 있음 (offset이 달라지므로)"""
    schedules = [get_weekday_schedule(2026, m, MEMBERS) for m in range(1, 13)]
    # 12개월 중 최소 2가지 이상의 다른 스케줄이 있어야 함
    unique = len(set(tuple(s.items()) for s in schedules))
    assert unique >= 2


# --- fixed_days 테스트 ---


def test_fixed_days_assigns_in_order():
    """fixed_days=3이면 월/화/수가 members[0]/[1]/[2]에 고정 배정"""
    schedule = get_weekday_schedule(2026, 4, MEMBERS, fixed_days=3)
    assert schedule[0] == MEMBERS[0]  # 월 -> U_A
    assert schedule[1] == MEMBERS[1]  # 화 -> U_B
    assert schedule[2] == MEMBERS[2]  # 수 -> U_C


def test_fixed_days_rotation_for_remaining():
    """fixed_days=3이면 목/금만 로테이션으로 배정"""
    schedule = get_weekday_schedule(2026, 4, MEMBERS, fixed_days=3)
    # 목/금은 로테이션 오프셋에 따라 결정
    offset = _get_rotation_offset(2026, 4, len(MEMBERS))
    assert schedule[3] == MEMBERS[(0 + offset) % len(MEMBERS)]  # 목
    assert schedule[4] == MEMBERS[(1 + offset) % len(MEMBERS)]  # 금


def test_fixed_days_consistency():
    """같은 월에서 fixed_days 스케줄은 항상 동일"""
    s1 = get_weekday_schedule(2026, 4, MEMBERS, fixed_days=3)
    s2 = get_weekday_schedule(2026, 4, MEMBERS, fixed_days=3)
    assert s1 == s2


def test_todays_deployer_with_fixed_days():
    """fixed_days 적용 시 오늘의 배포 담당자가 올바르게 반환"""
    # 2026-04-06은 월요일 (weekday=0), fixed_days=3이면 members[0] 고정
    with patch("service.deployment_rotation.date") as mock_date, patch(
        "service.deployment_rotation.is_business_day", return_value=True
    ):
        mock_date.today.return_value = date(2026, 4, 6)
        result = get_todays_deployer(MEMBERS, fixed_days=3)
        assert result == MEMBERS[0]


def test_fixed_days_zero_is_full_rotation():
    """fixed_days=0이면 기존 전체 로테이션과 동일"""
    schedule_default = get_weekday_schedule(2026, 4, MEMBERS)
    schedule_zero = get_weekday_schedule(2026, 4, MEMBERS, fixed_days=0)
    assert schedule_default == schedule_zero
