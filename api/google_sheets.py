"""
Google Sheets API 래퍼 함수
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

# 파일 목록을 이름으로 찾으려면 Drive 스코프가 필요하다. 읽기 전용은 유지한다 --
# 쓰기를 얹으면 에이전트가 사람이 관리하는 시트를 고칠 수 있게 되고, 되돌릴 수 없다.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _get_client() -> gspread.Client:
    """환경 변수에서 서비스 계정 JSON을 읽어 gspread 클라이언트 생성"""
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet_values(
    spreadsheet_id: str,
    worksheet_id: int | None = None,
    value_render_option: str = "FORMATTED_VALUE",
) -> list[list]:
    """
    워크시트의 모든 셀 값을 반환한다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        worksheet_id: 워크시트(탭) ID. 생략하면 첫 번째 탭.
            시트 링크에 #gid= 가 없으면 탭을 알 수 없다.
        value_render_option: "FORMATTED_VALUE" | "UNFORMATTED_VALUE" | "FORMULA"
            (https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption)
    """
    gc = _get_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = (
        sh.get_worksheet(0)
        if worksheet_id is None
        else sh.get_worksheet_by_id(worksheet_id)
    )
    return ws.get_all_values(value_render_option=value_render_option)


def search_spreadsheets(name: str = "") -> list[dict]:
    """서비스 계정이 볼 수 있는 스프레드시트를 이름으로 찾는다.

    서비스 계정도 하나의 구글 계정이라, **공유받은 파일만** 보인다.
    공유되지 않은 시트는 검색에도 안 나오고 읽기도 거부된다 -- 이것이
    권한 경계다.

    Args:
        name: 찾을 이름 조각. 비우면 전부

    Returns:
        list[dict]: id·name (수정 시각이 최근인 순서)
    """
    gc = _get_client()
    want = name.strip().lower()
    files = gc.list_spreadsheet_files()
    return [
        {"id": item["id"], "name": item["name"]}
        for item in files
        if not want or want in item["name"].lower()
    ]


def get_worksheet_titles(spreadsheet_id: str) -> list[dict]:
    """스프레드시트의 탭 목록을 반환한다.

    Args:
        spreadsheet_id: 스프레드시트 ID

    Returns:
        list[dict]: id·title
    """
    gc = _get_client()
    sh = gc.open_by_key(spreadsheet_id)
    return [{"id": ws.id, "title": ws.title} for ws in sh.worksheets()]
