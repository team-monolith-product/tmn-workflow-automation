import os
from datetime import datetime

import requests

from dotenv import load_dotenv
from slack_sdk import WebClient

import calendar

# 환경 변수 로드
load_dotenv()

CHANNEL_ID: str = 'C08EUJJSZF1'

def get_public_holidays(year: int, month: int):
    """
    DATA_GO_KR_SPECIAL_DAY_KEY 환경 변수에 등록된 서비스키를 사용하여,
    해당 연도, 월의 공휴일 정보를 getRestDeInfo API를 통해 조회하고,
    공휴일(공공기관 휴일여부가 'Y')인 날짜를 'YYYY-MM-DD' 형식의 문자열 집합으로 반환한다.
    """
    url = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
    params = {
        "solYear": str(year),
        "solMonth": f"{month:02d}",
        "ServiceKey": os.environ.get('DATA_GO_KR_SPECIAL_DAY_KEY'),
        "_type": "json",
        "numOfRows": "100"
    }
    response = requests.get(url, params=params, timeout=10)
    data = response.json()
    holidays = set()
    try:
        items = data['response']['body']['items']
        if 'item' in items:
            item = items['item']
            # 결과가 리스트인 경우
            if isinstance(item, list):
                for holiday in item:
                    if holiday.get('isHoliday') == "Y":
                        locdate = str(holiday.get('locdate'))
                        date_str = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}"
                        holidays.add(date_str)
            else:  # 단일 결과인 경우
                if item.get('isHoliday') == "Y":
                    locdate = str(item.get('locdate'))
                    date_str = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}"
                    holidays.add(date_str)
    except Exception as e:
        print("Error parsing holiday info:", e)
        print("Response data:", data)
    return holidays


def main():
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    email_to_slack_id = get_slack_user_map(slack_client)

    # 당월 1일부터 전날까지 날짜에 대해
    # 각 사용자의 근무 시간을 조회합니다.
    email_to_worktime = {}
    today = datetime.today()
    for i in range(1, today.day):
        date = today.replace(day=i).strftime('%Y-%m-%d')
        worktime = get_wantedspace_worktime(date)
        for result in worktime.get('results', []):
            email = result.get('email')
            if email:
                email_to_worktime[email] = email_to_worktime.get(
                    email, 0) + result.get('wk_time', 0)

    # 해당 월에 개인이 근무해야 하는 총 시간을 계산합니다.
    # 예를 들어, 평일(월~금)에 8시간씩 근무한다고 가정합니다.
    year = today.year
    month = today.month

    # 해당 월의 공휴일(휴일로 지정된 날)을 조회합니다.
    holidays = get_public_holidays(year, month)
    _, last_day = calendar.monthrange(year, month)
    required_daily_minutes = 8 * 60  # 하루에 요구되는 근무시간 (분)
    total_required_worktime = 0
    for day in range(1, last_day + 1):
        date_obj = today.replace(day=day)
        date_str = date_obj.strftime('%Y-%m-%d')
        if date_obj.weekday() < 5 and date_str not in holidays:
            total_required_worktime += required_daily_minutes

    slack_id_to_user_info = {
        slack_id: slack_client.users_info(user=slack_id)['user'] for slack_id in email_to_slack_id.values()
    }

    # 오늘을 포함한 남은 영업일(평일) 수 계산
    remaining_business_days = 0
    for day in range(today.day, last_day + 1):
        date_obj = today.replace(day=day)
        date_str = date_obj.strftime('%Y-%m-%d')
        if date_obj.weekday() < 5 and date_str not in holidays:
            remaining_business_days += 1

    # 각 사용자에 대해 (누적 근무시간 / 총 요구 근무시간)과
    # 남은 영업일 동안 평균적으로 요구되는 근무시간을 계산하여 메시지 병합
    messages = []
    for email, actual_worktime in email_to_worktime.items():
        slack_id = email_to_slack_id.get(email)
        if slack_id:
            user_info = slack_id_to_user_info.get(slack_id, {})
            real_name = user_info.get('real_name', 'Unknown')
            # 남은 요구 근무시간 (음수 방지를 위해 0 이하인 경우 0으로 처리)
            remaining_required = max(
                total_required_worktime - actual_worktime, 0)
            # 남은 영업일 평균 요구 근무시간 (분 단위를 시간 단위로 변환)
            avg_required = (
                remaining_required / remaining_business_days) if remaining_business_days > 0 else 0

            messages.append(
                f"{real_name} : 잔여 일평균 근로 시간: {avg_required/60:.1f} 시간 ({actual_worktime/60:.1f} 시간 / {total_required_worktime/60:.1f} 시간)"
            )

    # 메시지를 병합하여 한 번의 API 요청으로 전송
    messages.sort(key=lambda msg: msg.split(" : ")[0])
    if messages:
        full_message = "\n".join(messages)
        slack_client.chat_postMessage(channel=CHANNEL_ID, text=full_message)


def get_wantedspace_worktime(date: str):
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
    url = 'https://api.wantedspace.ai/tools/openapi/worktime/'
    query = {
        'date': date,
        'key': os.environ.get('WANTEDSPACE_API_KEY')
    }
    headers = {
        'Authorization': os.environ.get('WANTEDSPACE_API_SECRET')
    }
    response = requests.get(url, params=query, headers=headers, timeout=10)
    return response.json()


def get_slack_user_map(slack_client: WebClient):
    email_to_slack_id = {}
    cursor = None

    while True:
        response = slack_client.users_list(cursor=cursor)
        members = response["members"]

        for member in members:
            profile = member.get("profile", {})
            email = profile.get("email")
            if email:
                email_to_slack_id[email] = member["id"]

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return email_to_slack_id


if __name__ == "__main__":
    main()
