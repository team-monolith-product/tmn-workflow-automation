"""
공휴일 서비스 테스트
공공데이터포털 API에 누락된 휴일의 수동 보정 로직 검증
"""

from unittest.mock import patch

from service.holidays import get_public_holidays


def _empty_response():
    return {"response": {"body": {"items": {}}}}


@patch("service.holidays.get_rest_de_info")
def test_labor_day_added_every_year(mock_get_rest_de_info):
    """근로자의 날(5/1)은 매년 수동으로 추가되어야 함"""
    mock_get_rest_de_info.return_value = _empty_response()

    holidays = get_public_holidays(2025, 5)

    assert "2025-05-01" in holidays


@patch("service.holidays.get_rest_de_info")
def test_2026_08_substitute_holiday_added(mock_get_rest_de_info):
    """2026년 8월은 광복절 대체공휴일(8/17)이 API 누락분으로 수동 추가되어야 함"""
    mock_get_rest_de_info.return_value = _empty_response()

    holidays = get_public_holidays(2026, 8)

    assert "2026-08-17" in holidays


@patch("service.holidays.get_rest_de_info")
def test_2026_08_correction_is_year_specific(mock_get_rest_de_info):
    """8/17 보정은 2026년에 한정되어야 하며 다른 연도의 8월에는 적용되지 않아야 함"""
    mock_get_rest_de_info.return_value = _empty_response()

    holidays = get_public_holidays(2027, 8)

    assert "2027-08-17" not in holidays
