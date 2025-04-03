"""
data.go.kr API 모듈
"""

import os
import requests


def get_rest_de_info(year: int, month: int):
    """
    Args:
        year (int): 조회하고자 하는 연도 (YYYY)
        month (int): 조회하고자 하는 월 (1-12)
    """
    url = (
        "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
    )
    params = {
        "solYear": str(year),
        "solMonth": f"{month:02d}",
        "ServiceKey": os.environ.get("DATA_GO_KR_SPECIAL_DAY_KEY"),
        "_type": "json",
        "numOfRows": "100",
    }
    response = requests.get(url, params=params, timeout=10)
    return response.json()