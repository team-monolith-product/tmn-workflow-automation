"""탭 목록 테스트 — 숨긴 탭을 어디까지 보여 줄지는 read_headers 한 곳에서 정한다."""

from service.sheets import tabs


def _fake(monkeypatch, sheets):
    """메타데이터를 갈아끼우고, batch 로 읽힌 탭 이름을 기록한다."""
    asked: list[str] = []
    monkeypatch.setattr(
        tabs, "get_spreadsheet_metadata", lambda spreadsheet_id: {"sheets": sheets}
    )

    def header_rows(spreadsheet_id, titles):
        asked.extend(titles)
        return {"valueRanges": [{"values": [["성함"]]} for _ in titles]}

    monkeypatch.setattr(tabs, "get_header_rows", header_rows)
    return asked


def test_숨긴_탭은_카탈로그에_안_싣는다(monkeypatch):
    """사람이 감춰 둔 탭의 열 이름이 query_knowledge 검색 결과로 나가면 안 된다.

    실측(8/26)에 "예산 1차 변경(대외비)" 같은 탭이 hidden 으로 있었다.
    """
    asked = _fake(
        monkeypatch,
        [
            {"properties": {"sheetId": 0, "title": "명단"}},
            {"properties": {"sheetId": 9, "title": "대외비", "hidden": True}},
        ],
    )

    result = tabs.read_headers("X")

    assert [tab["title"] for tab in result] == ["명단"]
    assert asked == ["명단"], "숨긴 탭까지 읽으면 안 된다"


def test_링크가_가리킨_숨긴_탭은_목록에_넣는다(monkeypatch):
    # 사람이 링크로 직접 보여 준 탭이다. 다른 숨긴 탭은 여전히 뺀다.
    _fake(
        monkeypatch,
        [
            {"properties": {"sheetId": 0, "title": "명단"}},
            {"properties": {"sheetId": 9, "title": "연락처", "hidden": True}},
            {"properties": {"sheetId": 8, "title": "대외비", "hidden": True}},
        ],
    )

    result = tabs.read_headers("X", shown_gid=9)

    assert [tab["title"] for tab in result] == ["명단", "연락처"]


def test_빈_탭은_머리행이_빈_목록이다(monkeypatch):
    monkeypatch.setattr(
        tabs,
        "get_spreadsheet_metadata",
        lambda spreadsheet_id: {
            "sheets": [{"properties": {"sheetId": 0, "title": "빈"}}]
        },
    )
    monkeypatch.setattr(
        tabs, "get_header_rows", lambda spreadsheet_id, titles: {"valueRanges": [{}]}
    )

    assert tabs.read_headers("X") == [{"id": 0, "title": "빈", "header": []}]
