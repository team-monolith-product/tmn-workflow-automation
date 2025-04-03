import calendar
import os
import argparse
from datetime import datetime, timedelta
import time

import requests
from requests import Response
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import tabulate
from tabulate import tabulate

from service.slack import get_email_to_slack_id

# wide chars 모드 활성화 (한글 폭 계산에 wcwidth 사용)
tabulate.WIDE_CHARS_MODE = True

# 환경 변수 로드
load_dotenv()

CHANNEL_ID: str = "C08EUJJSZF1"
REQUIRED_DAILY_MINUTES = 8 * 60  # 하루 근무시간(480분)


def main():
    """
    --dry-run 옵션이 주어지면 실제 메시지 전송 없이 콘솔에만 출력합니다.

    변경점:
      - 컬럼 헤더를 짧게: ["이름", "잔여", "오늘", "예정"]
      - 세로줄 없이, 각 행 사이 가로줄만 표시
    """

    parser = argparse.ArgumentParser(description="근무 시간 알림 스크립트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다.",
    )
    args = parser.parse_args()

    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # 1) Slack 이메일→사용자ID 매핑
    email_to_slack_id = get_email_to_slack_id(slack_client)

    # 오늘(시분초=0)
    today = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)

    # 2) 이번 달 1일부터 "어제"까지 근무시간(WorkTime) 조회
    email_to_worktime = {}
    for day in range(1, today.day):
        date_str = today.replace(day=day).strftime("%Y-%m-%d")
        time.sleep(2)  # rate-limit 대비
        worktime_data = get_wantedspace_worktime(date_str)
        for result in worktime_data.get("results", []):
            email = result.get("email")
            if email:
                prev_val = email_to_worktime.get(email, 0)
                email_to_worktime[email] = prev_val + result.get("wk_time", 0)

    # 3) 월간 총 요구 근무시간 (평일 x 8h, 공휴일 제외)
    year, month = today.year, today.month
    holidays = get_public_holidays(year, month)
    _, last_day = calendar.monthrange(year, month)
    total_required_worktime = 0
    for d in range(1, last_day + 1):
        dt = today.replace(day=d)
        if dt.weekday() < 5 and dt.strftime("%Y-%m-%d") not in holidays:
            total_required_worktime += REQUIRED_DAILY_MINUTES

    # 4) Slack 사용자 상세정보
    slack_id_to_user_info = {}
    for slack_id in email_to_slack_id.values():
        time.sleep(2)
        try:
            resp = slack_call_with_retry(slack_client.users_info, user=slack_id)
            slack_id_to_user_info[slack_id] = resp["user"]
        except Exception as e:
            print(f"[WARN] Slack users_info failed for {slack_id}: {e}")
            slack_id_to_user_info[slack_id] = {}

    # 5) 사용자별 로직 → ASCII 테이블
    table_data = []
    for email, slack_id in email_to_slack_id.items():
        if not slack_id:
            continue
        user_info = slack_id_to_user_info.get(slack_id, {})
        real_name = user_info.get("real_name", "")
        if not real_name:
            continue

        # 이미 근무한 시간(분)
        actual_worktime = email_to_worktime.get(email, 0)

        # 휴가 (이미 사용, 오늘, 미래)
        time.sleep(2)
        vac_info = get_monthly_vacation_breakdown(email, year, month)
        used_vac = vac_info["used_days"]
        today_vac = vac_info["today_days"]
        future_vac = vac_info["future_days"]
        all_vac_days = used_vac + today_vac + future_vac

        # (월 요구시간) - (휴가×8h)
        adjusted_required_time = total_required_worktime - (
            all_vac_days * REQUIRED_DAILY_MINUTES
        )
        if adjusted_required_time < 0:
            adjusted_required_time = 0

        # 남은 근무시간(분)
        remaining_time = adjusted_required_time - actual_worktime
        if remaining_time < 0:
            remaining_time = 0

        # partial 휴가 → 남은 영업일
        time.sleep(2)
        daily_vac_map = get_daily_vacation_map(email, year, month)
        leftover_business_days = 0.0
        for d in range(today.day, last_day + 1):
            dt = today.replace(day=d)
            if dt.weekday() < 5 and dt.strftime("%Y-%m-%d") not in holidays:
                vac_fraction = daily_vac_map.get(d, 0.0)
                if vac_fraction > 1.0:
                    vac_fraction = 1.0
                leftover_business_days += 1.0 - vac_fraction

        avg_required_hours = 0.0
        if leftover_business_days > 0:
            possible_minutes = leftover_business_days * REQUIRED_DAILY_MINUTES
            avg_required_hours = (remaining_time / possible_minutes) * 8.0
            if avg_required_hours < 0:
                avg_required_hours = 0.0

        # 오늘 휴가 표기
        if today_vac == 0:
            today_vac_str = ""
        elif today_vac < 1:
            today_vac_str = "반차"
        else:
            today_vac_str = "휴가"

        # 예정 휴가
        if future_vac == 0:
            future_vac_str = ""
        else:
            future_vac_str = f"{future_vac:.1f} 일"

        table_data.append(
            [
                real_name,
                f"{avg_required_hours:.1f}h",  # "잔여" 열을 더 짧게 표기( 3.5h )
                today_vac_str,
                future_vac_str,
            ]
        )

    # 이름 순 정렬
    table_data.sort(key=lambda row: row[0])

    # 표 헤더를 최대한 짧게
    headers = ["이름", "잔여", "오늘", "예정"]
    basic_table = tabulate(table_data, headers=headers, tablefmt="simple")

    # 세로줄 없이, 각 행 뒤에 가로줄 추가
    ascii_table = insert_horizontal_lines(basic_table)

    if table_data:
        if args.dry_run:
            print("=== DRY RUN MODE (short headers, row lines) ===")
            print(f"채널: {CHANNEL_ID}")
            print("결과:\n", ascii_table)
            print("==========================================")
        else:
            code_block = f"```{ascii_table}```"
            try:
                slack_call_with_retry(
                    slack_client.chat_postMessage,
                    channel=CHANNEL_ID,
                    text="근무 현황(간소화)",
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": code_block},
                        }
                    ],
                )
                print("Slack 메시지 전송 완료.")
            except Exception as e:
                print("[ERROR] Slack post failed:", e)
    else:
        print("No data to display.")


def insert_horizontal_lines(table_str: str) -> str:
    """
    simple 포맷 + 각 행 사이에 가로줄 삽입
    """
    lines = table_str.split("\n")
    if not lines:
        return table_str

    max_len = max(len(ln) for ln in lines)
    line_sep = "-" * max_len

    new_lines = []
    for ln in lines:
        new_lines.append(ln)
        new_lines.append(line_sep)

    return "\n".join(new_lines)


def get_wantedspace_worktime(date: str):
    """
    특정 날짜(YYYY-MM-DD)에 대한 출퇴근(WorkTime) 조회.
    """
    url = "https://api.wantedspace.ai/tools/openapi/worktime/"
    query = {"date": date, "key": os.environ.get("WANTEDSPACE_API_KEY")}
    headers = {"Authorization": os.environ.get("WANTEDSPACE_API_SECRET")}

    r = requests_get_with_retry(url, params=query, headers=headers)
    return r.json() if r else {}


def get_public_holidays(year: int, month: int):
    """
    공공데이터포털 API로 해당 연/월의 공휴일(YYYY-MM-DD) 집합을 조회
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
    r = requests_get_with_retry(url, params=params)
    if not r:
        return set()

    data = r.json()
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
    return holidays


def get_monthly_vacation_breakdown(email: str, year: int, month: int):
    """
    이달의 휴가(연차·반차 등)
    - used_days (이미 사용)
    - today_days (오늘)
    - future_days (앞으로)
    """
    url = "https://api.wantedspace.ai/tools/openapi/workevent/"
    query = {
        "key": os.environ.get("WANTEDSPACE_API_KEY"),
        "date": f"{year}-{month:02d}-01",
        "type": "month",
        "email": email,
    }
    headers = {"Authorization": os.environ.get("WANTEDSPACE_API_SECRET")}

    r = requests_get_with_retry(url, params=query, headers=headers)
    if not r:
        return {"used_days": 0.0, "today_days": 0.0, "future_days": 0.0}

    data = r.json()

    today = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    _, last_day = calendar.monthrange(year, month)
    first_day_dt = datetime(year, month, 1)
    last_day_dt = datetime(year, month, last_day)

    day_to_vac_fraction = {d: 0.0 for d in range(1, last_day + 1)}

    try:
        results = data.get("results", [])
        for ev in results:
            s_str = ev.get("wk_start_date")
            e_str = ev.get("wk_end_date")
            counted = float(ev.get("wk_counted_days", 0.0))

            if not s_str or not e_str:
                continue

            s_dt = datetime.strptime(s_str, "%Y-%m-%d")
            e_dt = datetime.strptime(e_str, "%Y-%m-%d")

            if e_dt < first_day_dt or s_dt > last_day_dt:
                continue

            total_days = (e_dt - s_dt).days + 1
            if total_days <= 0:
                continue

            per_day_fraction = counted / total_days
            dt_cursor = s_dt
            while dt_cursor <= e_dt:
                if first_day_dt <= dt_cursor <= last_day_dt:
                    d_num = dt_cursor.day
                    day_to_vac_fraction[d_num] += per_day_fraction
                    if day_to_vac_fraction[d_num] > 1.0:
                        day_to_vac_fraction[d_num] = 1.0
                dt_cursor += timedelta(days=1)

    except Exception as e:
        print("[ERROR] Parsing vacation info:", e)
        print("Response data:", data)

    used_days = 0.0
    today_days = 0.0
    future_days = 0.0

    for d in range(1, last_day + 1):
        frac = day_to_vac_fraction[d]
        if frac <= 0:
            continue

        dt = datetime(year, month, d)
        if dt < today:
            used_days += frac
        elif dt == today:
            today_days += frac
        else:
            future_days += frac

    return {
        "used_days": used_days,
        "today_days": today_days,
        "future_days": future_days,
    }


def get_daily_vacation_map(email: str, year: int, month: int):
    """
    일자별 휴가 fraction -> { 1: 0.5, 2:1.0, ...}
    """
    url = "https://api.wantedspace.ai/tools/openapi/workevent/"
    query = {
        "key": os.environ.get("WANTEDSPACE_API_KEY"),
        "date": f"{year}-{month:02d}-01",
        "type": "month",
        "email": email,
    }
    headers = {"Authorization": os.environ.get("WANTEDSPACE_API_SECRET")}

    r = requests_get_with_retry(url, params=query, headers=headers)
    if not r:
        return {}

    data = r.json()
    _, last_day = calendar.monthrange(year, month)
    day_to_vac = {d: 0.0 for d in range(1, last_day + 1)}

    try:
        results = data.get("results", [])
        for ev in results:
            s_str = ev.get("wk_start_date")
            e_str = ev.get("wk_end_date")
            counted = float(ev.get("wk_counted_days", 0.0))
            if not s_str or not e_str:
                continue

            s_dt = datetime.strptime(s_str, "%Y-%m-%d")
            e_dt = datetime.strptime(e_str, "%Y-%m-%d")

            first_day_dt = datetime(year, month, 1)
            last_day_dt = datetime(year, month, last_day)
            if e_dt < first_day_dt or s_dt > last_day_dt:
                continue

            total_days = (e_dt - s_dt).days + 1
            if total_days <= 0:
                continue

            per_day_fraction = counted / total_days
            dt_cursor = s_dt
            while dt_cursor <= e_dt:
                if first_day_dt <= dt_cursor <= last_day_dt:
                    d_day = dt_cursor.day
                    day_to_vac[d_day] += per_day_fraction
                    if day_to_vac[d_day] > 1.0:
                        day_to_vac[d_day] = 1.0
                dt_cursor += timedelta(days=1)

    except Exception as e:
        print("[ERROR] in get_daily_vacation_map:", e)
        print("Response data:", data)

    return day_to_vac


def get_slack_user_map(slack_client: WebClient):
    """
    Slack 워크스페이스 전체 사용자를 조회한 뒤,
    이메일->Slack ID 매핑 딕셔너리를 반환
    """
    email_to_slack_id = {}
    cursor = None
    while True:
        time.sleep(2)
        try:
            resp = slack_call_with_retry(slack_client.users_list, cursor=cursor)
        except Exception as e:
            print(f"[ERROR] Slack users_list failed: {e}")
            break

        members = resp["members"]
        for m in members:
            profile = m.get("profile", {})
            email = profile.get("email")
            if email:
                email_to_slack_id[email] = m["id"]

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return email_to_slack_id


def requests_get_with_retry(
    url: str, params=None, headers=None, max_retries=3, initial_backoff=5
) -> Response | None:
    """
    requests.get에 대한 재시도 로직.
    - HTTP 429(Too Many Requests) 등에 대응
    """
    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
        except Exception as e:
            print(f"[WARN] requests.get exception on attempt={attempt}: {e}")
            if attempt == max_retries:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        if r.status_code == 429:
            print(f"[WARN] HTTP 429 Too Many Requests, attempt={attempt}")
            if attempt == max_retries:
                return None
            time.sleep(backoff)
            backoff *= 2
        elif not r.ok:
            print(f"[WARN] HTTP {r.status_code}, attempt={attempt}, reason={r.reason}")
            if attempt == max_retries:
                return None
            time.sleep(backoff)
            backoff *= 2
        else:
            return r
    return None


def slack_call_with_retry(slack_method, max_retries=3, initial_backoff=5, **kwargs):
    """
    Slack SDK 메서드(users_info, chat_postMessage 등) 재시도 로직
    """
    from slack_sdk.errors import SlackApiError

    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            return slack_method(**kwargs)
        except SlackApiError as e:
            if "rate_limited" in str(e) or "429" in str(e):
                print(f"[WARN] Slack Rate Limit, attempt={attempt}, error={e}")
                if attempt == max_retries:
                    raise
                time.sleep(backoff)
                backoff *= 2
            else:
                raise
        except Exception as ex:
            print(f"[WARN] Slack call exception on attempt={attempt}: {ex}")
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 5
    return None


if __name__ == "__main__":
    main()
