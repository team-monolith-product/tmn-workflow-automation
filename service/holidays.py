"""
공휴일 서비스

공공데이터포털 API를 사용하여 공휴일 정보를 조회합니다.
"""

from datetime import date

from api.data_go_kr import get_rest_de_info


def get_public_holidays(year: int, month: int) -> set[str]:
    """
    공공데이터포털 API로 해당 연/월의 공휴일(YYYY-MM-DD) 집합을 조회

    Args:
        year: 조회 연도
        month: 조회 월 (1-12)

    Returns:
        set[str]: 공휴일 날짜 집합 (예: {"2025-05-01", "2025-05-05"})

    Note:
        - 근로자의 날(5/1)은 공공데이터에 없으므로 수동 추가
    """
    data = get_rest_de_info(year, month)
    holidays = set()
    try:
        items = data["response"]["body"]["items"]
        if "item" in items:
            item = items["item"]
            if isinstance(item, list):
                for holiday in item:
                    if holiday.get("isHoliday") == "Y":
                        locdate = str(holiday["locdate"])
                        date_str = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}"
                        holidays.add(date_str)
            else:
                # 단일 item
                if item.get("isHoliday") == "Y":
                    locdate = str(item["locdate"])
                    date_str = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}"
                    holidays.add(date_str)
    except Exception as e:
        print("[ERROR] Parsing holiday info:", e)
        print("Response data:", data)

    # 수동으로 특정 휴일 추가
    if month == 5:
        # 근로자의 날 추가 (5월 1일)
        holidays.add(f"{year}-05-01")

    return holidays


def is_public_holiday(target_date: date) -> bool:
    """
    특정 날짜가 공휴일인지 확인

    Args:
        target_date: 확인할 날짜

    Returns:
        bool: 공휴일 여부
    """
    holidays = get_public_holidays(target_date.year, target_date.month)
    return target_date.isoformat() in holidays
