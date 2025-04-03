import calendar
import os
import argparse
from datetime import datetime
import time

from dotenv import load_dotenv
from slack_sdk import WebClient
from tabulate import tabulate

from api.data_go_kr import get_rest_de_info
from api.wantedspace import get_workevent
from service.slack import get_email_to_slack_id

# 환경 변수 로드
load_dotenv()

CHANNEL_ID: str = "C08EUJJSZF1"


def main():
    """
    --dry-run
      옵션이 주어지는 경우 실제 메시지를 전송하지 않고,
      대신 콘솔에 출력합니다.
    """
    # 명령행 인자 파싱
    parser = argparse.ArgumentParser(description="근무 시간 알림 스크립트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에 출력합니다.",
    )
    args = parser.parse_args()

    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    email_to_slack_id = get_email_to_slack_id(slack_client)

    # 당월 1일부터 전날까지 날짜에 대해
    # 각 사용자의 근무 시간을 조회합니다.
    email_to_worktime = {}
    today = datetime.today()
    for i in range(1, today.day):
        date = today.replace(day=i).strftime("%Y-%m-%d")
        worktime = get_worktime(date)
        for result in worktime.get("results", []):
            email = result.get("email")
            if email:
                email_to_worktime[email] = email_to_worktime.get(email, 0) + result.get(
                    "wk_time", 0
                )

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
        date_str = date_obj.strftime("%Y-%m-%d")
        if date_obj.weekday() < 5 and date_str not in holidays:
            total_required_worktime += required_daily_minutes

    slack_id_to_user_info = {
        slack_id: slack_client.users_info(user=slack_id)["user"]
        for slack_id in email_to_slack_id.values()
    }

    # 오늘을 포함한 남은 영업일(평일) 수 계산
    remaining_business_days = 0
    for day in range(today.day, last_day + 1):
        date_obj = today.replace(day=day)
        date_str = date_obj.strftime("%Y-%m-%d")
        if date_obj.weekday() < 5 and date_str not in holidays:
            remaining_business_days += 1

    # 각 사용자에 대해 (누적 근무시간 / 총 요구 근무시간)과
    # 남은 영업일 동안 평균적으로 요구되는 근무시간을 계산하여 메시지 병합
    table = []
    for email, slack_id in email_to_slack_id.items():
        actual_worktime = email_to_worktime.get(email, 0)
        if slack_id:
            user_info = slack_id_to_user_info.get(slack_id, {})
            real_name = user_info.get("real_name")

            if not real_name:
                # 이름이 조회되지 않는 경우는 비활성화된 사용자로 간주
                continue

            # 해당 사용자의 휴가 일수를 조회 (휴가 당 하루 근무시간 만큼 차감)
            vacation_days = get_vacation_days(email, year, month)
            # API Rate Limit을 고려하여 4초 대기
            time.sleep(5)

            # 각 사용자의 조정된 요구 근무시간: 기본 요구 근무시간에서 (휴가일수 × 하루 근무시간)을 차감
            adjusted_required = max(
                total_required_worktime - (vacation_days * required_daily_minutes), 0
            )
            remaining_required = max(adjusted_required - actual_worktime, 0)
            avg_required = (
                (remaining_required / remaining_business_days)
                if remaining_business_days > 0
                else 0
            )

            table.append(
                [
                    real_name,
                    f"{avg_required/60:.1f} 시간",
                    f"{actual_worktime/60:.1f} 시간",
                    f"{adjusted_required/60:.1f} 시간",
                    f"{vacation_days:.2f} 일",
                ]
            )

    # 표 형태로 출력
    table.sort(key=lambda row: row[0])
    if table:
        full_message = tabulate(
            table, headers=["성명", "잔여 시간", "수행 시간", "전체 시간", "휴가"]
        )
        if args.dry_run:
            print("=== DRY RUN MODE (메시지는 실제로 전송되지 않습니다) ===")
            print(f"채널: {CHANNEL_ID}")
            print(f"메시지 내용:\n{full_message}")
            print("===============================================")
        else:
            slack_client.chat_postMessage(
                channel=CHANNEL_ID, text=f"```{full_message}```"
            )


def get_public_holidays(year: int, month: int):
    """
    DATA_GO_KR_SPECIAL_DAY_KEY 환경 변수에 등록된 서비스키를 사용하여,
    해당 연도, 월의 공휴일 정보를 getRestDeInfo API를 통해 조회하고,
    공휴일(공공기관 휴일여부가 'Y')인 날짜를 'YYYY-MM-DD' 형식의 문자열 집합으로 반환한다.
    """
    data = get_rest_de_info(year, month)
    holidays = set()
    try:
        items = data["response"]["body"]["items"]
        if "item" in items:
            item = items["item"]
            # 결과가 리스트인 경우
            if isinstance(item, list):
                for holiday in item:
                    if holiday.get("isHoliday") == "Y":
                        locdate = str(holiday.get("locdate"))
                        date_str = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}"
                        holidays.add(date_str)
            else:  # 단일 결과인 경우
                if item.get("isHoliday") == "Y":
                    locdate = str(item.get("locdate"))
                    date_str = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}"
                    holidays.add(date_str)
    except Exception as e:
        print("Error parsing holiday info:", e)
        print("Response data:", data)
    return holidays


def get_vacation_days(email: str, year: int, month: int) -> float:
    """
    오픈API key와 시크릿을 사용하여, 근태 이벤트 API에서 지정한 이메일의
    해당 월 휴가(예: 연차, 보상휴가 등) 사용일수(wk_counted_days)를 조회하고 합산하여 반환한다.

    날짜는 해당 월의 첫째 날을 기준으로 조회하며, API 파라미터 type은 'month'를 사용한다.
    """
    data = get_workevent(date=f"{year}-{month:02d}-01", type="month", email=email)
    total_days = 0.0
    try:
        results = data["results"]

        encountered_pairs = set()
        for event in results:
            # 2일 이상의 휴가는 배열에 여러 번 나타납니다.
            # 시작일과 종료일이 같은 경우 중복을 피하기 위해 짝을 만들어서 중복을 체크합니다.
            event_name = event.get("wk_event_name")
            start = event.get("wk_start_date")
            end = event.get("wk_end_date")
            pair = (event_name, start, end)
            if pair in encountered_pairs:
                continue
            encountered_pairs.add(pair)
            total_days += float(event.get("wk_counted_days", 0))
    except Exception as e:
        print("Error parsing vacation info for", email, e)
        print("Response data:", data)
    return total_days


if __name__ == "__main__":
    main()
