"""
배포 담당자 로테이션 서비스

요일별 배포 담당자를 round-robin으로 계산합니다.
"""

from datetime import date

from service.business_days import is_business_day

WEEKDAY_NAMES = ["월", "화", "수", "목", "금"]


def _get_rotation_offset(year: int, month: int, num_members: int) -> int:
    """월별 로테이션 오프셋 계산"""
    return (year * 12 + month) % num_members


def get_weekday_schedule(
    year: int, month: int, members: list[str], fixed_days: int = 0
) -> dict[int, str]:
    """해당 월의 요일별 배포 담당자 매핑 반환

    Args:
        year: 연도
        month: 월
        members: 배포 담당자 Slack user ID 목록
        fixed_days: 고정 요일 수 (0부터 시작, members 순서대로 고정)

    Returns:
        dict[int, str]: 요일 인덱스(0=월..4=금) -> Slack user ID
    """
    schedule = {}
    # 고정 요일: members[i]를 요일 i에 배정
    for i in range(min(fixed_days, 5)):
        schedule[i] = members[i % len(members)]

    # 로테이션 요일
    rotation_days = list(range(fixed_days, 5))
    if rotation_days:
        offset = _get_rotation_offset(year, month, len(members))
        for idx, weekday in enumerate(rotation_days):
            schedule[weekday] = members[(idx + offset) % len(members)]

    return schedule


def get_todays_deployer(members: list[str], fixed_days: int = 0) -> str | None:
    """오늘의 배포 담당자를 반환. 영업일이 아니면 None.

    Args:
        members: 배포 담당자 Slack user ID 목록
        fixed_days: 고정 요일 수 (0부터 시작, members 순서대로 고정)

    Returns:
        str | None: 오늘의 배포 담당자 Slack user ID, 영업일이 아니면 None
    """
    today = date.today()
    if not is_business_day(today):
        return None
    weekday = today.weekday()  # 0=월..4=금
    if weekday >= 5:
        return None
    schedule = get_weekday_schedule(today.year, today.month, members, fixed_days)
    return schedule[weekday]
