"""탭 해석 테스트 — 엉뚱한 탭을 읽으면 숫자는 나오는데 다른 표의 숫자다."""

import inspect

import gspread
import pytest

from api import google_sheets


class FakeTab:
    def __init__(self, tab_id, title):
        self.id = tab_id
        self.title = title


class FakeSheet:
    """gspread.Spreadsheet 흉내. worksheets 와 get_worksheet_by_id 만 쓴다."""

    def __init__(self, tabs):
        self.tabs = tabs

    def worksheets(self):
        return self.tabs

    def get_worksheet_by_id(self, tab_id):
        for tab in self.tabs:
            if tab.id == tab_id:
                return tab
        raise KeyError(tab_id)


# gid 2025 인 탭과, 제목이 "2025" 인 다른 탭이 함께 있는 시트.
SHEET = FakeSheet([FakeTab(2025, "명단"), FakeTab(55, "2025")])


def test_링크에서_온_gid는_이름으로_풀지_않는다():
    # int 는 #gid= 에서 왔으므로 모호하지 않다. 이것까지 이름 우선으로 풀면
    # gid 2025 를 가리킨 링크가 "2025" 라는 **탭**을 열어 버린다.
    assert google_sheets._worksheet(SHEET, 2025).title == "명단"


def test_사람이_준_숫자_문자열은_탭_이름이_먼저다():
    # "2025", "1학기" 처럼 숫자로만 된 탭 이름이 흔하다. 숫자를 gid 로 먼저 보면
    # 그런 탭은 영영 못 열고, 사람은 gid 를 적은 적이 없어 원인을 알 수 없다.
    assert google_sheets._worksheet(SHEET, "2025").id == 55


def test_이름이_없으면_그때_gid로_본다():
    assert google_sheets._worksheet(SHEET, "55").title == "2025"


def test_없는_탭이면_탭_목록을_알려준다():
    # 파이썬 내부 메시지로는 무엇을 고쳐야 하는지 알 수 없다.
    with pytest.raises(ValueError, match="명단, 2025"):
        google_sheets._worksheet(SHEET, "없는탭")


def test_gspread_저수준_표면이_그대로다():
    """requirements 가 gspread 를 무핀으로 두므로, 재빌드가 버전을 올릴 수 있다.

    이 셋이 바뀌면 30분마다 도는 잡만 조용히 죽는다 -- 나머지 테스트는 전부
    fake 로 이 표면을 대체하므로 CI 는 초록이다.
    """
    http = gspread.http_client.HTTPClient

    assert list(inspect.signature(http.fetch_sheet_metadata).parameters) == [
        "self",
        "id",
        "params",
    ]
    assert list(inspect.signature(http.values_batch_get).parameters) == [
        "self",
        "id",
        "ranges",
        "params",
    ]
    assert hasattr(gspread.utils, "absolute_range_name")


def test_숨긴_탭은_카탈로그에_안_싣는다(monkeypatch):
    """사람이 감춰 둔 탭의 열 이름이 query_knowledge 검색 결과로 나가면 안 된다.

    실측(8/26)에 "예산 1차 변경(대외비)" 같은 탭이 hidden 으로 있었다.
    """

    class FakeHTTP:
        def fetch_sheet_metadata(self, spreadsheet_id, params=None):
            return {
                "sheets": [
                    {"properties": {"sheetId": 0, "title": "명단"}},
                    {"properties": {"sheetId": 9, "title": "대외비", "hidden": True}},
                ]
            }

        def values_batch_get(self, spreadsheet_id, ranges, params=None):
            assert len(ranges) == 1, "숨긴 탭까지 읽으면 안 된다"
            return {"valueRanges": [{"values": [["성함", "전화"]]}]}

    class FakeClient:
        http_client = FakeHTTP()

    monkeypatch.setattr(google_sheets, "_get_client", lambda: FakeClient())

    tabs = google_sheets.get_worksheet_headers("X")

    assert [tab["title"] for tab in tabs] == ["명단"]
