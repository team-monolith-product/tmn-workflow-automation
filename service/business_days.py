"""
영업일 계산 서비스

주말과 공휴일을 고려한 영업일 계산 기능을 제공합니다.
"""

import calendar
from datetime import date, timedelta

from service.holidays import get_public_holidays


def is_business_day(target_date: date, exclude_holidays: bool = True) -> bool:
    """
    특정 날짜가 영업일인지 확인

    Args:
        target_date: 확인할 날짜
        exclude_holidays: True면 공휴일도 제외

    Returns:
        bool: 영업일 여부
    """
    # 주말 제외 (토, 일 = 5, 6)
    if target_date.weekday() >= 5:
        return False

    # 공휴일 제외
    if exclude_holidays:
        holidays = get_public_holidays(target_date.year, target_date.month)
        if target_date.isoformat() in holidays:
            return False

    return True


def count_business_days(
    start_date: date,
    end_date: date,
    exclude_holidays: bool = True,
) -> int:
    """
    두 날짜 사이의 영업일 수 계산 (start_date 포함, end_date 미포함)

    Args:
        start_date: 시작 날짜
        end_date: 종료 날짜
        exclude_holidays: True면 공휴일도 제외

    Returns:
        int: 영업일 수 (start_date > end_date면 음수 반환)
    """
    if start_date > end_date:
        return -count_business_days(end_date, start_date, exclude_holidays)

    # 공휴일 캐싱 (월별로 한 번만 조회)
    holidays_cache: dict[tuple[int, int], set[str]] = {}

    def get_holidays_cached(year: int, month: int) -> set[str]:
        key = (year, month)
        if key not in holidays_cache:
            holidays_cache[key] = get_public_holidays(year, month)
        return holidays_cache[key]

    business_days = 0
    current_date = start_date

    while current_date < end_date:
        # 주말 제외
        if current_date.weekday() < 5:
            # 공휴일 제외
            if exclude_holidays:
                holidays = get_holidays_cached(current_date.year, current_date.month)
                if current_date.isoformat() not in holidays:
                    business_days += 1
            else:
                business_days += 1
        current_date += timedelta(days=1)

    return business_days


def get_business_days_in_range(
    start_date: date,
    end_date: date,
    exclude_holidays: bool = True,
) -> list[date]:
    """
    두 날짜 사이의 영업일 목록 반환 (start_date 포함, end_date 미포함)

    Args:
        start_date: 시작 날짜
        end_date: 종료 날짜
        exclude_holidays: True면 공휴일도 제외

    Returns:
        list[date]: 영업일 목록
    """
    # 공휴일 캐싱
    holidays_cache: dict[tuple[int, int], set[str]] = {}

    def get_holidays_cached(year: int, month: int) -> set[str]:
        key = (year, month)
        if key not in holidays_cache:
            holidays_cache[key] = get_public_holidays(year, month)
        return holidays_cache[key]

    business_days = []
    current_date = start_date

    while current_date < end_date:
        if current_date.weekday() < 5:
            if exclude_holidays:
                holidays = get_holidays_cached(current_date.year, current_date.month)
                if current_date.isoformat() not in holidays:
                    business_days.append(current_date)
            else:
                business_days.append(current_date)
        current_date += timedelta(days=1)

    return business_days


def count_business_days_in_month(
    year: int,
    month: int,
    exclude_holidays: bool = True,
) -> int:
    """
    특정 월의 총 영업일 수 계산

    Args:
        year: 연도
        month: 월
        exclude_holidays: True면 공휴일도 제외

    Returns:
        int: 해당 월의 영업일 수
    """
    _, last_day = calendar.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day) + timedelta(days=1)

    return count_business_days(start_date, end_date, exclude_holidays)
