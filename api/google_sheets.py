"""
Google Sheets API 래퍼 함수
"""

import json
import os
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

KST = timezone(timedelta(hours=9))

# Google Sheets 날짜 기준일 (1899-12-30)
_SHEETS_EPOCH = datetime(1899, 12, 30, tzinfo=KST)


def _get_client() -> gspread.Client:
    """환경 변수에서 서비스 계정 JSON을 읽어 gspread 클라이언트 생성"""
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _serial_to_date(serial: int | float) -> datetime | None:
    """Google Sheets 날짜 시리얼 값을 datetime으로 변환"""
    if not serial:
        return None
    return _SHEETS_EPOCH + timedelta(days=int(serial))


def _serial_to_time(serial: int | float) -> tuple[int, int] | None:
    """Google Sheets 시간 시리얼 값(하루의 비율)을 (hour, minute) 튜플로 변환"""
    if not serial:
        return None
    total_minutes = round(serial * 1440)
    return (total_minutes // 60, total_minutes % 60)


def read_school_schedules(spreadsheet_id: str, worksheet_id: int) -> list[dict]:
    """
    학교별 수업 일정을 읽어온다.

    시트 스키마: 학교명 | 날짜1 | 시작1 | 종료1 | 날짜2 | 시작2 | 종료2 | ...
    UNFORMATTED_VALUE로 읽어 날짜는 시리얼 숫자, 시간은 하루 비율로 반환됨.

    Args:
        spreadsheet_id: Google Sheets 파일 ID
        worksheet_id: 시트(탭) ID

    Returns:
        [
            {
                "school_name": "선인고등학교",
                "schedules": [
                    {"date": datetime, "start_hour": 13, "start_min": 30, "end_hour": 17, "end_min": 30},
                    ...
                ]
            },
            ...
        ]
    """
    gc = _get_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.get_worksheet_by_id(worksheet_id)
    rows = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")

    results = []
    for row in rows[1:]:  # 헤더 스킵
        school_name = str(row[0]).strip() if row else ""
        if not school_name:
            continue

        schedules = []
        # 컬럼 1부터 3개씩 (날짜, 시작, 종료)
        col = 1
        while col + 2 < len(row):
            date_val = row[col]
            start_val = row[col + 1]
            end_val = row[col + 2]

            if not date_val:
                col += 3
                continue

            date = _serial_to_date(date_val)
            start = _serial_to_time(start_val)
            end = _serial_to_time(end_val)

            if date and start and end:
                schedules.append(
                    {
                        "date": date,
                        "start_hour": start[0],
                        "start_min": start[1],
                        "end_hour": end[0],
                        "end_min": end[1],
                    }
                )

            col += 3

        results.append({"school_name": school_name, "schedules": schedules})

    return results
