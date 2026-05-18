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


def get_worksheet_values(spreadsheet_id: str, worksheet_id: int) -> list[list]:
    """
    워크시트의 모든 셀 값을 UNFORMATTED_VALUE 형태로 반환한다.
    날짜는 시리얼 숫자, 시간은 하루의 비율(0.0~1.0)로 반환됨.
    """
    gc = _get_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.get_worksheet_by_id(worksheet_id)
    return ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
