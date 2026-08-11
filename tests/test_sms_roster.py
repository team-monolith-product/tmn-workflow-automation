"""채널-참가자시트 연결 테스트"""

import pytest

from service.sms import roster


def test_주소를_붙여넣어도_ID를_뽑는다():
    # 사람은 보통 주소창을 통째로 붙여넣는다.
    assert (
        roster.parse_spreadsheet_id(
            "https://docs.google.com/spreadsheets/d/1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd/edit#gid=0"
        )
        == "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_ID를_그대로_줘도_받는다():
    assert (
        roster.parse_spreadsheet_id("1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd")
        == "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_앞뒤_공백은_무시한다():
    assert roster.parse_spreadsheet_id("  1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd \n") == (
        "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_시트가_아니면_거절한다():
    # 조용히 통과시키면 엉뚱한 ID 로 시트를 열려다 나중에 죽는다.
    with pytest.raises(ValueError):
        roster.parse_spreadsheet_id("그 시트요")


def test_짧은_문자열은_ID로_보지_않는다():
    with pytest.raises(ValueError):
        roster.parse_spreadsheet_id("abc123")
