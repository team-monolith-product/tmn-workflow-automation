"""
Google Sheets API 래퍼 함수
"""

import json
import os
import threading

import gspread
from google.oauth2.service_account import Credentials
from gspread.urls import DRIVE_FILES_API_V3_URL
from gspread.utils import absolute_range_name

# 파일 목록을 이름으로 찾으려면 Drive 스코프가, 셀을 읽으려면 Sheets 스코프가 필요하다.
# 둘 다 읽기 전용으로 둔다 -- 쓰기를 얹으면 에이전트가 사람이 관리하는 시트를 고칠 수
# 있게 되고, 되돌릴 방법이 없다.
SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

_client: gspread.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> gspread.Client:
    """gspread 클라이언트를 만들어 재사용한다.

    호출마다 만들면 시트 하나당 OAuth 토큰 grant 가 한 번씩 더 나가고 커넥션도
    재사용되지 않는다. 카탈로그 동기화는 한 번에 수십 개 시트를 훑으므로 그만큼
    쿼터를 태운다.

    재시도는 붙이지 않는다. 429 는 여기서 예외가 아니라 평상시라, 물러서서
    기다리면 코드 실행 워커(하나뿐이다)를 붙잡아 봇 넷이 같이 선다.
    쿼터는 부르는 쪽이 READS_PER_MINUTE 로 스스로를 눌러 지킨다.
    """
    global _client
    with _client_lock:
        if _client is None:
            info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
            _client = gspread.authorize(credentials)
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


# Sheets 읽기 쿼터. 서비스 계정 하나가 곧 한 명이라 이 프로세스 전체가 이 값을
# 나눠 쓴다. 여럿을 도는 쪽(sync_sheet_catalog)이 이 둘로 스스로를 눌러야 한다.
# 여기서 재우지 않는 이유는, 그러면 사람이 부른 read_sheet 가 동기화 뒤에 줄을
# 서기 때문이다.
READS_PER_MINUTE = 60
READS_PER_SHEET = 2  # get_worksheet_headers 한 번에 드는 호출 수


def _worksheet(sheet: gspread.Spreadsheet, want: str | int) -> gspread.Worksheet:
    """탭 이름이나 gid 로 탭 하나를 고른다. 문자열이면 **이름을 먼저 본다.**

    이 판단이 service 가 아니라 여기 있는 이유는 살아 있는 탭 목록이 있어야
    풀리기 때문이다. 위로 올리면 왕복이 한 번 더 들거나 gspread 객체가 밖으로
    샌다. "이 API 를 부르려면 무엇을 넘겨야 하나" 를 푸는 어댑터의 일이다.

    Args:
        sheet: 열린 스프레드시트
        want: int 면 gid 가 확실하다(링크에서 뽑은 값). str 이면 사람이 준 것이라
            이름일 수도 gid 일 수도 있어 이름을 먼저 본다 -- "2025", "1학기" 처럼
            숫자로만 된 탭 이름이 흔한데, 숫자를 gid 로 먼저 보면 그런 탭은 영영
            못 열고 사람은 gid 를 적은 적이 없어 원인을 알 수 없다
    """
    text = str(want).strip()
    tabs = sheet.worksheets()
    # int 는 링크의 #gid= 에서 왔으므로 모호하지 않다. 이것까지 이름으로 먼저
    # 풀면, gid 2025 를 가리키는 링크가 "2025" 라는 **탭**을 열어 버린다.
    if isinstance(want, int):
        for worksheet in tabs:
            if worksheet.id == want:
                return worksheet
    else:
        for worksheet in tabs:
            if worksheet.title == text:
                return worksheet
        if text.lstrip("-").isdigit():
            for worksheet in tabs:
                if worksheet.id == int(text):
                    return worksheet
    # gspread 의 get_worksheet_by_id 를 안 쓰는 이유는 이 안내 때문이다. 그쪽은
    # "id … not found" 만 던지는데, 지워진 탭을 가리키는 옛 링크가 흔해서
    # 사람에게는 "그럼 어느 탭이 있나" 가 필요하다.
    raise ValueError(
        f"'{text}' 라는 탭이 없습니다."
        f" 이 시트의 탭: {', '.join(worksheet.title for worksheet in tabs)}"
    )


def get_worksheet_values(
    spreadsheet_id: str,
    worksheet: str | int | None = None,
    value_render_option: str = "FORMATTED_VALUE",
) -> list[list]:
    """
    워크시트의 모든 셀 값을 반환한다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        worksheet: 탭 이름 또는 gid. 생략하면 첫 번째 탭.
            시트 링크에 #gid= 가 없으면 탭을 알 수 없다.
        value_render_option: "FORMATTED_VALUE" | "UNFORMATTED_VALUE" | "FORMULA"
            (https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption)
    """
    sheet = _get_client().open_by_key(spreadsheet_id)
    tab = sheet.get_worksheet(0) if worksheet is None else _worksheet(sheet, worksheet)
    return tab.get_all_values(value_render_option=value_render_option)


def get_worksheet_headers(spreadsheet_id: str) -> list[dict]:
    """탭마다 머리행(1행)만 읽는다. 시트 하나에 **READS_PER_SHEET 회**가 든다.

    탭 수만큼 values.get 을 부르면 시트 하나에 1+N 회가 든다. 94개 시트에
    탭 평균 셋이면 30분마다 370회고, Sheets 읽기 쿼터는 사용자당 분당 60회다.
    batch 로 묶는다.

    **Spreadsheet 객체를 안 만든다.** gspread 의 open_by_key 는 생성자에서
    fetch_sheet_metadata 를 부르고 worksheets() 가 같은 것을 또 부른다. 즉
    같은 GET 이 두 번 나가 시트당 3회가 된다(8/26 실측). 메타데이터를 한 번만
    받아 직접 읽으면 2회다.

    예외를 삼키지 않는다. 머리행을 못 읽은 탭을 빈 값으로 적재하면 카탈로그가
    "이 탭에는 열이 없다"를 최신 정보인 척 들고 있게 됩니다.

    Args:
        spreadsheet_id: 스프레드시트 ID

    Returns:
        list[dict]: id·title·header
    """
    http = _get_client().http_client
    metadata = http.fetch_sheet_metadata(spreadsheet_id)
    # 숨긴 탭은 뺀다. 사람이 시트에서 감춰 둔 것을 카탈로그에 실으면 그 열 이름이
    # query_knowledge 검색 결과로 나가고, 봇을 부를 수 있는 사람 누구나 gid 로 셀까지
    # 읽는다. 실측(8/26)에 "예산 1차 변경(대외비)" 같은 탭이 있었다.
    tabs = [
        entry["properties"]
        for entry in metadata.get("sheets", [])
        if not entry["properties"].get("hidden")
    ]
    if not tabs:
        return []

    # A1 표기에서 탭 이름 안의 작은따옴표는 두 번 겹쳐 써야 한다. 직접 f-string 으로
    # 감싸면 "김'철수 명단" 같은 탭에서 range 가 깨져 400 이 나고, 그 시트는 사람이
    # 탭 이름을 고치기 전까지 영영 카탈로그에 못 들어간다.
    result = http.values_batch_get(
        spreadsheet_id,
        [absolute_range_name(tab["title"], "1:1") for tab in tabs],
        params={"majorDimension": "ROWS"},
    )
    ranges = result.get("valueRanges", [])
    return [
        {
            "id": tab["sheetId"],
            "title": tab["title"],
            # 빈 탭은 values 키 자체가 없다.
            "header": (value_range.get("values") or [[]])[0],
        }
        # strict -- 응답이 요청보다 짧으면 뒤쪽 탭이 조용히 사라지는 대신 터진다.
        for tab, value_range in zip(tabs, ranges, strict=True)
    ]
