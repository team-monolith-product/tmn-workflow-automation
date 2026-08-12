"""
Google Sheets API 래퍼 함수
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

# 발송 이력을 시트에 적으려면 쓰기가 필요하다. 서비스 계정은 사업 폴더에
# 이미 편집자로 공유되어 있어 권한 자체는 있었고, 스코프가 좁혀 두고 있었다.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_client() -> gspread.Client:
    """환경 변수에서 서비스 계정 JSON을 읽어 gspread 클라이언트 생성"""
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet(spreadsheet_id: str) -> gspread.Spreadsheet:
    """스프레드시트를 연다.

    어느 탭을 쓸지 고르는 것은 호출하는 기능의 결정이라 여기서 하지 않는다.

    Args:
        spreadsheet_id: 스프레드시트 ID

    Returns:
        gspread.Spreadsheet: 스프레드시트
    """
    return _get_client().open_by_key(spreadsheet_id)


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
