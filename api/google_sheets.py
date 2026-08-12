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


def get_first_worksheet(
    spreadsheet_id: str, worksheet_name: str | None = None
) -> gspread.Worksheet:
    """이름으로 워크시트를 가져온다. 이름을 안 주면 첫 번째 탭.

    Args:
        spreadsheet_id: 스프레드시트 ID
        worksheet_name: 탭 이름. None 이면 첫 번째 탭

    Returns:
        gspread.Worksheet: 워크시트
    """
    sh = _get_client().open_by_key(spreadsheet_id)
    return sh.worksheet(worksheet_name) if worksheet_name else sh.get_worksheet(0)


def get_worksheet(spreadsheet_id: str, worksheet_name: str) -> gspread.Worksheet:
    """이름으로 워크시트를 가져온다. 없으면 만든다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        worksheet_name: 탭 이름

    Returns:
        gspread.Worksheet: 워크시트
    """
    sh = _get_client().open_by_key(spreadsheet_id)
    try:
        return sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=worksheet_name, rows=1000, cols=20)


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
