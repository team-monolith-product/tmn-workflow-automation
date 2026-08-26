"""탭 해석 테스트 — 엉뚱한 탭을 읽으면 숫자는 나오는데 다른 표의 숫자다."""

import inspect

import gspread
import pytest

from api import google_sheets


class FakeTab:
    def __init__(self, tab_id, title, hidden=False):
        self.id = tab_id
        self.title = title
        self.isSheetHidden = hidden


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
    """requirements 의 gspread 핀(~=6.2)이 지키는 것을 여기서도 확인한다.

    이 셋이 바뀌면 30분마다 도는 잡만 조용히 죽는다 -- 나머지 테스트는 전부
    fake 로 이 표면을 대체하므로 CI 는 초록이다. 핀 범위 안에서 마이너가
    올라가도 걸리도록 남겨 둔다.
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

    monkeypatch.setattr(google_sheets, "_get_client", lambda account=None: FakeClient())

    tabs = google_sheets.get_worksheet_headers("X")

    assert [tab["title"] for tab in tabs] == ["명단"]


def test_안내문에_숨긴_탭_이름을_넣지_않는다():
    # 오타 한 번에 감춰 둔 탭 이름이 딸려 나가면 안 된다. 묻지도 않았는데
    # 봇이 먼저 알려 주는 셈이다.
    sheet = FakeSheet([FakeTab(0, "명단"), FakeTab(9, "예산(대외비)", hidden=True)])

    with pytest.raises(ValueError) as caught:
        google_sheets._worksheet(sheet, "없는탭")

    assert "명단" in str(caught.value)
    assert "대외비" not in str(caught.value)


def test_숨긴_탭도_직접_지목하면_열린다():
    # 막는 것은 발견이지 읽기가 아니다. gid 를 박아 둔 스크립트가 있고,
    # 링크를 직접 준 것은 의도적 접근이다.
    sheet = FakeSheet([FakeTab(0, "명단"), FakeTab(9, "예산(대외비)", hidden=True)])

    assert google_sheets._worksheet(sheet, 9).title == "예산(대외비)"
    assert google_sheets._worksheet(sheet, "예산(대외비)").id == 9


def test_탭이_전부_숨김이면_문장이_끊기지_않는다():
    # "이 시트의 탭: " 에서 끊기면 사람은 자기 링크가 잘못됐는지 시트가 빈 것인지
    # 알 수 없다.
    sheet = FakeSheet([FakeTab(9, "대외비", hidden=True)])

    with pytest.raises(ValueError, match="전부 숨김"):
        google_sheets._worksheet(sheet, "없는탭")
