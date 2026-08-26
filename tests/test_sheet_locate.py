"""시트 지목·주입 테스트 — 엉뚱한 시트를 골라 읽으면 안 된다."""

import pytest

from service.sheets import frame, locate

HITS = [
    {"id": "AAA", "name": "[기업연계] 부산 2기 만족도 조사 응답"},
    {"id": "BBB", "name": "부산 2기 만족도 조사 응답의 복사본"},
]
VALUES = [
    ["성함", "휴대전화 번호", "트랙"],
    ["가", "010-1111-1111", "A"],
    ["나", "010-2222-2222", "B"],
]


def _search(hits):
    return lambda name: hits


def test_링크는_찾지_않고_그대로_쓴다():
    called = []

    def search(name):
        called.append(name)
        return []

    found = locate.locate(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit#gid=7", search
    )

    assert found.sheet.spreadsheet_id == "1hW3Yg8x99gfiLd"
    assert found.sheet.worksheet_id == 7
    # 링크를 받고도 Drive 를 뒤지면 느려지기만 한다.
    assert called == []


def test_이름이_하나로_좁혀지면_읽는다():
    found = locate.locate("부산 만족도", _search(HITS[:1]))

    assert found.sheet.spreadsheet_id == "AAA"


def test_후보가_여럿이면_고르지_않는다():
    # "…의 복사본" 이 실제로 여럿 있다. 하나를 골라 읽으면 엉뚱한 명단으로
    # 문자가 나가고, 문장이 자연스러워서 아무도 못 잡는다.
    found = locate.locate("부산 만족도", _search(HITS))

    assert found.sheet is None
    assert len(found.candidates) == 2


def test_후보_목록에_이름과_id가_같이_나온다():
    # 이름만 주면 사람이 고른 뒤에도 다시 찾아야 한다.
    out = locate.render_candidates(HITS)

    assert "복사본" in out
    assert "AAA" in out and "BBB" in out


def test_못_찾으면_공유_여부를_짚어준다():
    # 서비스 계정에 공유가 안 된 시트는 검색에도 안 나온다. 그 사실을 안 알려주면
    # 이름을 바꿔가며 계속 다시 부른다.
    with pytest.raises(ValueError, match="공유"):
        locate.locate("없는시트", _search([]))


def test_빈_값은_거절한다():
    with pytest.raises(ValueError):
        locate.locate("   ", _search(HITS))


def test_주입된_read_sheet는_행을_dict로_준다():
    # pd.DataFrame(rows) 한 줄로 표가 되어야 한다.
    read_sheet = frame.build_read_sheet(_search(HITS[:1]), lambda sid, wid: VALUES)

    rows = read_sheet("부산 만족도")

    assert rows[0] == {"성함": "가", "휴대전화 번호": "010-1111-1111", "트랙": "A"}
    assert len(rows) == 2


def test_주입된_read_sheet는_자르지_않는다():
    # 결과가 컨텍스트가 아니라 실행 메모리로 간다. 자르면 잘린 표로 통계를 낸다.
    big = [["성함"]] + [[f"이름{i}"] for i in range(1, 3001)]
    read_sheet = frame.build_read_sheet(_search(HITS[:1]), lambda sid, wid: big)

    assert len(read_sheet("아무거나")) == 3000


def test_주입된_read_sheet도_열을_고른다():
    read_sheet = frame.build_read_sheet(_search(HITS[:1]), lambda sid, wid: VALUES)

    rows = read_sheet("아무거나", columns="성함, 전화")

    assert list(rows[0]) == ["성함", "휴대전화 번호"]


def test_tab을_주면_그_탭을_읽는다():
    seen = {}

    def fetch(sid, wid):
        seen["wid"] = wid
        return VALUES

    read_sheet = frame.build_read_sheet(_search(HITS[:1]), fetch)
    read_sheet("아무거나", tab="1270298877")

    assert seen["wid"] == 1270298877


def test_후보가_여럿이면_코드에서도_터진다():
    # 조용히 하나를 골라 통계를 내면, 숫자는 나오는데 다른 시트의 숫자다.
    read_sheet = frame.build_read_sheet(_search(HITS), lambda sid, wid: VALUES)

    with pytest.raises(ValueError, match="복사본"):
        read_sheet("부산 만족도")
