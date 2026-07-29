"""
Google Sheets API 래퍼 함수
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _get_client() -> gspread.Client:
    """환경 변수에서 서비스 계정 JSON을 읽어 gspread 클라이언트 생성"""
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
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
    gc = _get_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.get_worksheet_by_id(worksheet_id)
    return ws.get_all_values(value_render_option=value_render_option)


def list_worksheets(spreadsheet_id: str) -> list[dict]:
    """
    스프레드시트의 탭 목록과 각 탭의 크기를 반환한다.

    Returns:
        [{"title": str, "id": int, "row_count": int, "col_count": int}, ...]
    """
    gc = _get_client()
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
    gc = _get_client()
    sh = gc.open_by_key(spreadsheet_id)
    return sh.values_get(range_a1, params={"valueRenderOption": value_render_option})
