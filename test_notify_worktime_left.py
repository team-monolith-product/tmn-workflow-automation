"""
휴가 계산 로직 테스트
주말 포함 외근신청 시 평일만 계산되는지 검증
"""

from notify_worktime_left import get_monthly_vacation_breakdown, get_daily_vacation_map


def test_weekend_vacation_calculation():
    """주말 포함 휴가 신청 시 평일만 계산되는지 테스트"""
    # 2025년 1월 3일(금) ~ 6일(월) 2일 휴가 신청
    mock_workevent = {
        "results": [
            {
                "wk_start_date": "2025-01-03",  # 금요일
                "wk_end_date": "2025-01-06",  # 월요일
                "wk_counted_days": 2.0,  # 2일 휴가
            }
        ]
    }

    # 1월 3일이 금요일인 2025년 1월 테스트
    result = get_monthly_vacation_breakdown(2025, 1, mock_workevent)

    # 금요일 1일 + 월요일 1일 = 2일만 계산되어야 함
    assert result["used_days"] == 2.0, "주말 포함 휴가에서 평일만 계산되어야 함"


def test_daily_vacation_map_weekend():
    """일별 휴가 맵에서 주말 제외 확인"""
    mock_workevent = {
        "results": [
            {
                "wk_start_date": "2025-01-03",  # 금요일
                "wk_end_date": "2025-01-06",  # 월요일
                "wk_counted_days": 2.0,
            }
        ]
    }

    daily_map = get_daily_vacation_map(2025, 1, mock_workevent)

    # 3일(금): 1.0, 4일(토): 0.0, 5일(일): 0.0, 6일(월): 1.0
    assert daily_map[3] == 1.0, "금요일은 1일 휴가"
    assert daily_map[4] == 0.0, "토요일은 휴가 없음"
    assert daily_map[5] == 0.0, "일요일은 휴가 없음"
    assert daily_map[6] == 1.0, "월요일은 1일 휴가"


def test_weekday_only_vacation():
    """평일만 휴가 신청 시 정상 동작 확인"""
    mock_workevent = {
        "results": [
            {
                "wk_start_date": "2025-01-07",  # 화요일
                "wk_end_date": "2025-01-09",  # 목요일
                "wk_counted_days": 3.0,
            }
        ]
    }

    result = get_monthly_vacation_breakdown(2025, 1, mock_workevent)
    assert result["used_days"] == 3.0, "평일만 휴가 시 정상 계산"


def test_partial_vacation_with_weekend():
    """반차 포함 주말 휴가 테스트"""
    mock_workevent = {
        "results": [
            {
                "wk_start_date": "2025-01-03",  # 금요일
                "wk_end_date": "2025-01-06",  # 월요일
                "wk_counted_days": 1.0,  # 1일 휴가 (반차 2개)
            }
        ]
    }

    daily_map = get_daily_vacation_map(2025, 1, mock_workevent)

    # 1일 휴가를 2개 평일로 나누면 각각 0.5일
    assert daily_map[3] == 0.5, "금요일 반차"
    assert daily_map[4] == 0.0, "토요일 휴가 없음"
    assert daily_map[5] == 0.0, "일요일 휴가 없음"
    assert daily_map[6] == 0.5, "월요일 반차"


def test_weekend_only_vacation():
    """주말만 휴가 신청 시 (실제로는 발생하지 않지만 방어적 테스트)"""
    mock_workevent = {
        "results": [
            {
                "wk_start_date": "2025-01-04",  # 토요일
                "wk_end_date": "2025-01-05",  # 일요일
                "wk_counted_days": 2.0,
            }
        ]
    }

    result = get_monthly_vacation_breakdown(2025, 1, mock_workevent)
    daily_map = get_daily_vacation_map(2025, 1, mock_workevent)

    # 평일이 없으므로 휴가 적용 안됨
    assert result["used_days"] == 0.0, "주말만 신청 시 휴가 없음"
    assert daily_map[4] == 0.0, "토요일 휴가 없음"
    assert daily_map[5] == 0.0, "일요일 휴가 없음"
