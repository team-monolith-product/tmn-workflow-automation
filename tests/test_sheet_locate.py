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


def _files(items):
    return lambda: items


def test_링크는_찾지_않고_그대로_쓴다():
    called = []

    def list_files():
        called.append(1)
        return FILES

    found = locate.locate(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit#gid=7", list_files
    )

    assert found.sheet.spreadsheet_id == "1hW3Yg8x99gfiLd"
    assert found.sheet.worksheet_id == 7
    # 링크를 받고도 Drive 를 뒤지면 느려지기만 한다.
    assert called == []


def test_이름이_하나로_좁혀지면_읽는다():
    found = locate.locate("출석부", _files(FILES))

    assert found.sheet.spreadsheet_id == "CCC"


def test_후보가_여럿이면_고르지_않는다():
    # "…의 복사본" 이 실제로 여럿 있다. 하나를 골라 읽으면 엉뚱한 명단으로
    # 문자가 나가고, 문장이 자연스러워서 아무도 못 잡는다.
    found = locate.locate("부산 2기 만족도", _files(FILES))

    assert found.sheet is None
    assert [item["id"] for item in found.candidates] == ["AAA", "BBB"]


def test_대소문자를_가리지_않고_찾는다():
    files = [
        {"id": "X", "name": "Busan Survey", "modifiedTime": "2026-08-01T00:00:00Z"}
    ]

    assert locate.locate("busan", _files(files)).sheet.spreadsheet_id == "X"


def test_후보_목록에_이름과_id와_수정일이_같이_나온다():
    # 이름만 주면 사람이 고른 뒤에도 다시 찾아야 한다. 수정일은 사본 중
    # 어느 것이 살아 있는지 가르는 단서다.
    out = locate.render_candidates(FILES[:2])

    assert "복사본" in out
    assert "AAA" in out and "BBB" in out
    assert "2026-08-15" in out


def test_못_찾으면_공유_여부를_짚어준다():
    # 서비스 계정에 공유가 안 된 시트는 검색에도 안 나온다. 그 사실을 안 알려주면
    # 이름을 바꿔가며 계속 다시 부른다.
    with pytest.raises(ValueError, match="공유"):
        locate.locate("없는시트", _files([]))


def test_빈_값은_무엇이_필요한지_말한다():
    with pytest.raises(ValueError, match="링크나 시트 이름"):
        locate.locate("   ", _files(FILES))


def test_읽으면_행이_dict로_온다(monkeypatch):
    # pd.DataFrame(rows) 한 줄로 표가 되어야 한다.
    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES[2:]))
    monkeypatch.setattr(read, "get_worksheet_values", lambda sid, wid: VALUES)

    rows = read.read_sheet("출석부")

    assert rows[0] == {"성함": "가", "휴대전화 번호": "010-1111-1111", "트랙": "A"}
    assert len(rows) == 2


def test_읽을_때는_자르지_않는다(monkeypatch):
    # 결과가 컨텍스트가 아니라 실행 메모리로 간다. 자르면 잘린 표로 통계를 낸다.
    big = [["성함"]] + [[f"이름{i}"] for i in range(1, 3001)]
    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES[2:]))
    monkeypatch.setattr(read, "get_worksheet_values", lambda sid, wid: big)

    assert len(read.read_sheet("출석부")) == 3000


def test_읽을_때_열을_고른다(monkeypatch):
    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES[2:]))
    monkeypatch.setattr(read, "get_worksheet_values", lambda sid, wid: VALUES)

    rows = read.read_sheet("출석부", columns="성함, 전화")

    assert list(rows[0]) == ["성함", "휴대전화 번호"]


def test_tab에_gid를_주면_그_탭을_읽는다(monkeypatch):
    seen = {}

    def fetch(sid, wid):
        seen["wid"] = wid
        return VALUES

    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES[2:]))
    monkeypatch.setattr(read, "get_worksheet_values", fetch)
    read.read_sheet("출석부", tab="1270298877")

    assert seen["wid"] == 1270298877


def test_tab에_탭_이름을_줘도_읽는다(monkeypatch):
    # 카탈로그가 gid 와 탭 이름을 나란히 보여주므로 이름을 넣는 쪽이 자연스럽다.
    seen = {}

    def fetch(sid, wid):
        seen["wid"] = wid
        return VALUES

    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES[2:]))
    monkeypatch.setattr(read, "get_worksheet_values", fetch)
    monkeypatch.setattr(
        read,
        "get_worksheet_headers",
        lambda sid: [{"id": 77, "title": "공문신청", "header": []}],
    )

    read.read_sheet("출석부", tab="공문신청")

    assert seen["wid"] == 77


def test_없는_탭이면_탭_목록을_알려준다(monkeypatch):
    # int() 내부 메시지로는 무엇을 고쳐야 하는지 알 수 없다.
    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES[2:]))
    monkeypatch.setattr(
        read,
        "get_worksheet_headers",
        lambda sid: [{"id": 0, "title": "응답 시트1", "header": []}],
    )

    with pytest.raises(ValueError, match="응답 시트1"):
        read.read_sheet("출석부", tab="없는탭")


def test_후보가_여럿이면_읽기도_막힌다(monkeypatch):
    # 조용히 하나를 골라 통계를 내면, 숫자는 나오는데 다른 시트의 숫자다.
    monkeypatch.setattr(read, "list_spreadsheet_files", _files(FILES))

    with pytest.raises(ValueError, match="복사본"):
        read.read_sheet("부산 2기 만족도")
