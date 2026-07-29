"""
Google Sheets API 래퍼 함수
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

# 읽기 전용 경로와 쓰기 경로의 스코프를 나눈다.
# 기존 스크립트(scripts/discord_post_completion_notice.py)는 읽기만 하므로
# 봇이 쓰기 기능을 갖는다고 해서 함께 권한이 넓어지지 않게 한다.
READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
READWRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# 기존 호출부 호환용
SCOPES = READONLY_SCOPES


def _get_client(scopes: list[str]) -> gspread.Client:
    """환경 변수에서 서비스 계정 JSON을 읽어 gspread 클라이언트 생성"""
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def get_worksheet_values(
    spreadsheet_id: str,
    worksheet_id: int,
    value_render_option: str = "FORMATTED_VALUE",
) -> list[list]:
    """
    워크시트의 모든 셀 값을 반환한다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        worksheet_id: 워크시트(탭) ID
        value_render_option: "FORMATTED_VALUE" | "UNFORMATTED_VALUE" | "FORMULA"
            (https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption)
    """
    gc = _get_client(READONLY_SCOPES)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.get_worksheet_by_id(worksheet_id)
    return ws.get_all_values(value_render_option=value_render_option)


def list_worksheets(spreadsheet_id: str) -> list[dict]:
    """
    스프레드시트의 탭 목록과 각 탭의 크기를 반환한다.

    Returns:
        [{"title": str, "id": int, "row_count": int, "col_count": int}, ...]
    """
    gc = _get_client(READONLY_SCOPES)
    sh = gc.open_by_key(spreadsheet_id)
    return [
        {
            "title": ws.title,
            "id": ws.id,
            "row_count": ws.row_count,
            "col_count": ws.col_count,
        }
        for ws in sh.worksheets()
    ]


def get_range(
    spreadsheet_id: str,
    range_a1: str,
    value_render_option: str = "FORMATTED_VALUE",
) -> dict:
    """
    A1 표기 범위의 값을 조회한다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        range_a1: "시트1!A1:D20" 형태의 범위
        value_render_option: "FORMATTED_VALUE" | "UNFORMATTED_VALUE" | "FORMULA"

    Returns:
        Sheets API values.get 원본 응답
    """
    gc = _get_client(READONLY_SCOPES)
    sh = gc.open_by_key(spreadsheet_id)
    return sh.values_get(range_a1, params={"valueRenderOption": value_render_option})


def update_range(
    spreadsheet_id: str,
    range_a1: str,
    values: list[list],
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """
    A1 표기 범위에 값을 덮어쓴다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        range_a1: "시트1!A1:D20" 형태의 범위
        values: 행 단위 2차원 배열
        value_input_option: "USER_ENTERED"(수식·서식 해석) | "RAW"(문자열 그대로)

    Returns:
        Sheets API values.update 원본 응답
    """
    gc = _get_client(READWRITE_SCOPES)
    sh = gc.open_by_key(spreadsheet_id)
    return sh.values_update(
        range_a1,
        params={"valueInputOption": value_input_option},
        body={"values": values},
    )


def append_rows(
    spreadsheet_id: str,
    worksheet_title: str,
    values: list[list],
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """
    워크시트 맨 아래에 행을 추가한다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        worksheet_title: 탭 이름
        values: 행 단위 2차원 배열
        value_input_option: "USER_ENTERED" | "RAW"

    Returns:
        Sheets API values.append 원본 응답
    """
    gc = _get_client(READWRITE_SCOPES)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_title)
    return ws.append_rows(values, value_input_option=value_input_option)
