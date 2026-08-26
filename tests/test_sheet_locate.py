"""시트 지목·읽기 테스트 — 엉뚱한 시트를 골라 읽으면 안 된다."""

import pytest

from service.sheets import locate, read

FILES = [
    {
        "id": "AAA",
        "name": "[기업연계] 부산 2기 만족도 조사 응답",
        "modifiedTime": "2026-08-15T09:30:00Z",
    },
    {
        "id": "BBB",
        "name": "부산 2기 만족도 조사 응답의 복사본",
        "modifiedTime": "2026-08-14T01:00:00Z",
    },
    {"id": "CCC", "name": "출석부 정리", "modifiedTime": "2026-08-01T00:00:00Z"},
]
VALUES = [
    ["성함", "휴대전화 번호", "트랙"],
    ["가", "010-1111-1111", "A"],
    ["나", "010-2222-2222", "B"],
]


@pytest.fixture(autouse=True)
def _no_cache():
    """목록 캐시를 테스트 사이에 비웁니다."""
    locate._cache = None
    yield
    locate._cache = None


def _files(monkeypatch, items):
    monkeypatch.setattr(locate, "list_spreadsheet_files", lambda: items)


def test_링크는_찾지_않고_그대로_쓴다(monkeypatch):
    called = []

    def list_files():
        called.append(1)
        return FILES

    monkeypatch.setattr(locate, "list_spreadsheet_files", list_files)
    found = locate.locate(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit#gid=7"
    )

    assert found.sheet.spreadsheet_id == "1hW3Yg8x99gfiLd"
    assert found.sheet.worksheet_id == 7
    # 링크를 받고도 Drive 를 뒤지면 느려지기만 한다.
    assert called == []


def test_목록을_짧게_캐시한다(monkeypatch):
    # 에이전트가 시트 둘을 이름으로 대조하면 전량 목록이 그때마다 나간다.
    calls = []
    monkeypatch.setattr(
        locate, "list_spreadsheet_files", lambda: (calls.append(1), FILES)[1]
    )

    locate.locate("출석부")
    locate.locate("출석부")

    assert len(calls) == 1


def test_이름이_하나로_좁혀지면_읽는다(monkeypatch):
    _files(monkeypatch, FILES)
    found = locate.locate("출석부")

    assert found.sheet.spreadsheet_id == "CCC"


def test_후보가_여럿이면_고르지_않는다(monkeypatch):
    # "…의 복사본" 이 실제로 여럿 있다. 하나를 골라 읽으면 엉뚱한 명단으로
    # 문자가 나가고, 문장이 자연스러워서 아무도 못 잡는다.
    _files(monkeypatch, FILES)
    found = locate.locate("부산 2기 만족도")

    assert found.sheet is None
    assert [item["id"] for item in found.candidates] == ["AAA", "BBB"]


def test_대소문자를_가리지_않고_찾는다(monkeypatch):
    _files(
        monkeypatch,
        [{"id": "X", "name": "Busan Survey", "modifiedTime": "2026-08-01T00:00:00Z"}],
    )

    assert locate.locate("busan").sheet.spreadsheet_id == "X"


def test_후보_목록에_이름과_id와_수정일이_같이_나온다():
    # 이름만 주면 사람이 고른 뒤에도 다시 찾아야 한다. 수정일은 사본 중
    # 어느 것이 살아 있는지 가르는 단서다.
    out = locate.render_candidates(FILES[:2])

    assert "복사본" in out
    assert "AAA" in out and "BBB" in out
    assert "2026-08-15" in out


def test_못_찾으면_공유_여부를_짚어준다(monkeypatch):
    # 서비스 계정에 공유가 안 된 시트는 검색에도 안 나온다. 그 사실을 안 알려주면
    # 이름을 바꿔가며 계속 다시 부른다.
    _files(monkeypatch, [])
    with pytest.raises(ValueError, match="공유"):
        locate.locate("없는시트")


def test_빈_값은_무엇이_필요한지_말한다(monkeypatch):
    _files(monkeypatch, FILES)
    with pytest.raises(ValueError, match="링크나 시트 이름"):
        locate.locate("   ")


def test_읽으면_행이_dict로_온다(monkeypatch):
    # pd.DataFrame(rows) 한 줄로 표가 되어야 한다.
    _files(monkeypatch, FILES[2:])
    monkeypatch.setattr(read, "get_worksheet_values", lambda sid, tab=None: VALUES)

    rows = read.read_sheet("출석부")

    assert rows[0] == {"성함": "가", "휴대전화 번호": "010-1111-1111", "트랙": "A"}
    assert len(rows) == 2


def test_읽을_때는_자르지_않는다(monkeypatch):
    # 결과가 컨텍스트가 아니라 실행 메모리로 간다. 자르면 잘린 표로 통계를 낸다.
    big = [["성함"]] + [[f"이름{i}"] for i in range(1, 3001)]
    _files(monkeypatch, FILES[2:])
    monkeypatch.setattr(read, "get_worksheet_values", lambda sid, tab=None: big)

    assert len(read.read_sheet("출석부")) == 3000


def test_읽을_때_열을_고른다(monkeypatch):
    _files(monkeypatch, FILES[2:])
    monkeypatch.setattr(read, "get_worksheet_values", lambda sid, tab=None: VALUES)

    rows = read.read_sheet("출석부", columns="성함, 전화")

    # 부른 이름이 그대로 키가 된다. 코드가 바로 다음 줄에서 row["전화"] 를 쓴다.
    assert list(rows[0]) == ["성함", "전화"]
    assert rows[0]["전화"] == "010-1111-1111"


def test_tab을_주면_그대로_넘긴다(monkeypatch):
    seen = {}

    def fetch(sid, tab=None):
        seen["tab"] = tab
        return VALUES

    _files(monkeypatch, FILES[2:])
    monkeypatch.setattr(read, "get_worksheet_values", fetch)
    read.read_sheet("출석부", tab="1270298877")

    # 탭 이름이냐 gid 냐를 가르는 것은 api 계층이다(get_worksheet_values).
    # 여기서 int() 로 넘겨 버리면 "2025" 같은 **탭 이름**을 gid 로 읽는다.
    assert seen["tab"] == "1270298877"


def test_tab에_탭_이름을_줘도_그대로_넘긴다(monkeypatch):
    # 카탈로그가 gid 와 탭 이름을 나란히 보여주므로 이름을 넣는 쪽이 자연스럽다.
    seen = {}

    def fetch(sid, tab=None):
        seen["tab"] = tab
        return VALUES

    _files(monkeypatch, FILES[2:])
    monkeypatch.setattr(read, "get_worksheet_values", fetch)

    read.read_sheet("출석부", tab="공문신청")

    assert seen["tab"] == "공문신청"


def test_후보가_여럿이면_읽기도_막힌다(monkeypatch):
    # 조용히 하나를 골라 통계를 내면, 숫자는 나오는데 다른 시트의 숫자다.
    _files(monkeypatch, FILES)

    with pytest.raises(ValueError, match="복사본"):
        read.read_sheet("부산 2기 만족도")


def test_시트가_아닌_구글_링크는_공유_탓으로_돌리지_않는다(monkeypatch):
    # 구글 **문서** 링크를 붙여넣으면 "공유를 확인하십시오" 가 나가고, 사람은
    # 멀쩡한 공유 설정을 뒤진다. 진짜 원인은 그게 시트가 아니라는 것이다.
    _files(monkeypatch, FILES)

    with pytest.raises(ValueError, match="시트 링크가 아닙니다"):
        locate.locate("https://docs.google.com/document/d/1abcdefg/edit")


def test_스킴_없이_붙여넣은_시트_링크도_읽는다(monkeypatch):
    # 슬랙에서 복사하면 https:// 가 빠지는 일이 있다.
    _files(monkeypatch, FILES)

    found = locate.locate("docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit#gid=7")

    assert found.sheet == locate.Sheet("1hW3Yg8x99gfiLd", 7)
