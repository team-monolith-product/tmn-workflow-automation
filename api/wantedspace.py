"""
원티드 스페이스 관련 API 모듈
"""

import datetime
import os
import time
from typing import Literal

import requests


def get_workevent(
    date: str = datetime.datetime.now().strftime("%Y-%m-%d"),
    type: Literal["day", "week", "month", "year"] | None = None,
    email: str | None = None,
):
    """
    Args:
        date (str): 조회하고자 하는 날짜 (YYYY-MM-DD)
        type (str): 조회하고자 하는 기간 (day, week, month, year)
        email (str): 조회하고자 하는 사용자의 이메일 (optional)

    Returns:
        {
            "next": None,
            "previous": None,
            "count": 3,
            "results": [
                {
                    "wk_start_date": "2025-01-03",
                    "wk_end_date": "2025-01-03",
                    "event_name": "연차(오후)",
                    "wk_counted_days": 0.5,
                    "wk_alter_days": 0.0,
                    "wk_comp_days": 0.0,
                    "status": "INFORMED",
                    "wk_location": "",
                    "wk_comment": "",
                    "username": "김바바",
                    "email": "kpapa@team-mono.com",
                    "eid": "",
                    "evt_start_time": "13:00:00",
                    "evt_end_time": "17:00:00",
                    "wk_event": "WNS_VACATION_PM",
                    "applied_days": 1
                },
                ...
            ]
        }
    """
    url = "https://api.wantedspace.ai/tools/openapi/workevent/"
    query = {
        "date": date,
        "key": os.environ.get("WANTEDSPACE_API_KEY"),
    }
    if type:
        query["type"] = type
    if email:
        query["email"] = email
    headers = {"Authorization": os.environ.get("WANTEDSPACE_API_SECRET")}
    response = requests_get_with_retry(url, params=query, headers=headers)
    return response.json()


def get_worktime(date: str):
    """
    Args:
        date (str): 조회하고자 하는 날짜 (YYYY-MM-DD)

    Returns:
        {
            "next": null,
            "previous": null,
            "count": 2,
            "results": [
                {
                    "username": "김샘",
                    "email": "a25@abc.com",
                    "team_name": "AI팀",
                    "eid": "",
                    "wk_date": "2022-06-09",
                    "wk_start_time": "2022-06-09T09:00:00+09:00",
                    "wk_end_time": "2022-06-09T18:00:00+09:00",
                    "wk_start_time_sch": "2022-06-09T09:00:00+09:00",
                    "wk_end_time_sch": "2022-06-09T19:30:00+09:00",
                    "wk_time_except": 0,
                    "wk_time_except_legal": 60,
                    "wk_time": 480,
                    "wk_time_today": 480,
                    "memo": "",
                    "wk_approved": "APV_IN/APV_OUT",
                    "work_except": []
                },
                {
                    "username": "복봄",
                    "email": "a26@abc.com",
                    "team_name": "CEO",
                    "eid": "",
                    "wk_date": "2022-06-09",
                    "wk_start_time": "2022-06-09T09:00:00+09:00",
                    "wk_end_time": "2022-06-09T18:00:00+09:00",
                    "wk_start_time_sch": "2022-06-09T09:00:00+09:00",
                    "wk_end_time_sch": "2022-06-09T19:30:00+09:00",
                    "wk_time_except": 0,
                    "wk_time_except_legal": 65,
                    "wk_time": 475,
                    "wk_time_today": 475,
                    "memo": "",
                    "wk_approved": "APV_IN/APV_OUT",
                    "work_except": [
                        {
                            "wk_except_start_time": "2022-06-09T18:19:22.137414+09:00",
                            "wk_except_end_time": "2022-06-09T18:42:24.425192+09:00",
                            "wk_except_time_min": 23
                        }
                    ]
                },
            ]
        }
    """
    url = "https://api.wantedspace.ai/tools/openapi/worktime/"
    query = {"date": date, "key": os.environ.get("WANTEDSPACE_API_KEY")}
    headers = {"Authorization": os.environ.get("WANTEDSPACE_API_SECRET")}
    response = requests_get_with_retry(url, params=query, headers=headers)
    return response.json()


def requests_get_with_retry(
    url: str, params=None, headers=None, max_retries=3, initial_backoff=5
) -> requests.Response:
    """
    requests.get에 대한 재시도 로직.
    - HTTP 429(Too Many Requests) 등에 대응

    원티드스페이스는 429 응답을 반환함.
    """
    backoff = initial_backoff
    for _ in range(1, max_retries + 1):
        response = requests.get(
            url, params=params, headers=headers, timeout=10)

        if response.status_code == 429:
            time.sleep(backoff)
            backoff *= 2

        return response
    return requests.get(url, params=params, headers=headers, timeout=10)
