"""탭 해석 테스트 — 엉뚱한 탭을 읽으면 숫자는 나오는데 다른 표의 숫자다."""

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
