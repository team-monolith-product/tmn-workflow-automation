"""
출근자 조회 서비스

원티드스페이스 API를 사용하여 출근자 정보를 조회합니다.
"""

from datetime import date

from api.wantedspace import get_worktime


def get_working_emails(target_date: date | None = None) -> list[str]:
    """
    워티드스페이스 API를 통해 현재 출근한 사용자 이메일 목록을 반환

    Args:
        target_date: 조회할 날짜 (None이면 오늘)

    Returns:
        list[str]: 출근 중인 사용자 이메일 목록

    Note:
        - 휴가 중인 사용자는 get_worktime API에서 wk_start_time이 null로 반환됨
        - 출근한 사용자는 wk_start_time이 존재하고 퇴근하지 않은 경우 wk_end_time이 null임
        - 따라서 출근한 상태로 간주하기 위해서는 wk_start_time이 존재하고 wk_end_time이 null인지 확인
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()
    worktime = get_worktime(date=date_str)

    working_emails = []
    if worktime and "results" in worktime:
        for user in worktime["results"]:
            # 실제 출근 기록이 있고(wk_start_time이 존재), 아직 퇴근하지 않은(wk_end_time이 null) 사용자만 포함
            # 휴가자는 wk_start_time이 null이므로 자동으로 제외됨
            if user["wk_start_time"] is not None and user["wk_end_time"] is None:
                working_emails.append(user["email"])

    return working_emails
