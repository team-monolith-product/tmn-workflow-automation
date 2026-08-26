"""
Google Sheets API 래퍼 함수
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials
from gspread.urls import DRIVE_FILES_API_V3_URL

# 파일 목록을 이름으로 찾으려면 Drive 스코프가, 셀을 읽으려면 Sheets 스코프가 필요하다.
# 둘 다 읽기 전용으로 둔다 -- 쓰기를 얹으면 에이전트가 사람이 관리하는 시트를 고칠 수
# 있게 되고, 되돌릴 방법이 없다.
SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

_client: gspread.Client | None = None


def _get_client() -> gspread.Client:
    """gspread 클라이언트를 만들어 재사용한다.

    호출마다 만들면 시트 하나당 OAuth 토큰 grant 가 한 번씩 더 나가고 커넥션도
    재사용되지 않는다. 카탈로그 동기화는 한 번에 수십 개 시트를 훑으므로 그만큼
    쿼터를 태운다.

    BackOffHTTPClient 를 쓰는 이유는 429 다. 기본 클라이언트는 재시도가 없어서
    Sheets 읽기 쿼터(사용자당 분당 60회)에 걸리는 순간 그대로 예외가 된다.
    """
    global _client
    if _client is None:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
        _client = gspread.authorize(credentials, http_client=gspread.BackOffHTTPClient)
    return _client


SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


def list_spreadsheet_files() -> list[dict]:
    """서비스 계정이 볼 수 있는 스프레드시트를 나열한다. 휴지통은 뺀다.

    gspread 의 list_spreadsheet_files 를 쓰지 않는 이유는 그 쿼리에 trashed=false 가
    없어서다. Drive files.list 는 기본으로 휴지통 파일을 포함하므로, 사본을 지워
    정리해도 계속 후보에 섞이고 ID 로는 읽히기까지 한다. 대신 gspread 의 HTTP
    클라이언트를 그대로 쓰므로 인증·재시도·공유 드라이브 플래그는 같이 따라온다.

    **공유 드라이브 플래그가 필수다.** 실측(8/21)에서 보이는 시트 94개가 전부 공유
    드라이브 소속이었고, 그 플래그 없이 부르면 0개가 나온다.

    Returns:
        list[dict]: id·name·modifiedTime·webViewLink. 최근 수정 순
    """
    client = _get_client()
    params = {
        "q": f"mimeType='{SPREADSHEET_MIME}' and trashed=false",
        "pageSize": 1000,
        "orderBy": "modifiedTime desc",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink)",
    }
    files: list[dict] = []
    while True:
        payload = client.http_client.request(
            "get", DRIVE_FILES_API_V3_URL, params=params
        ).json()
        files.extend(payload.get("files", []))
        token = payload.get("nextPageToken")
        if not token:
            return files
        params["pageToken"] = token


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


def get_worksheet_headers(spreadsheet_id: str) -> list[dict]:
    """탭마다 머리행(1행)만 읽는다.

    탭 수만큼 values.get 을 부르면 시트 하나에 1+N 회가 든다. 94개 시트에
    탭 평균 셋이면 30분마다 370회고, Sheets 읽기 쿼터는 사용자당 분당 60회다.
    batch 로 묶어 시트당 2회로 줄인다.

    예외를 삼키지 않는다. 머리행을 못 읽은 탭을 빈 값으로 적재하면 카탈로그가
    "이 탭에는 열이 없다"를 최신 정보인 척 들고 있게 되고, 커서가 이미 지나가서
    그 시트가 다시 수정될 때까지 복구되지 않는다.

    Args:
        spreadsheet_id: 스프레드시트 ID

    Returns:
        list[dict]: id·title·header
    """
    sheet = _get_client().open_by_key(spreadsheet_id)
    worksheets = sheet.worksheets()
    if not worksheets:
        return []

    result = sheet.values_batch_get(
        [f"'{ws.title}'!1:1" for ws in worksheets],
        params={"majorDimension": "ROWS"},
    )
    ranges = result.get("valueRanges", [])
    return [
        {
            "id": ws.id,
            "title": ws.title,
            # 빈 탭은 values 키 자체가 없다.
            "header": (value_range.get("values") or [[]])[0],
        }
        for ws, value_range in zip(worksheets, ranges)
    ]
