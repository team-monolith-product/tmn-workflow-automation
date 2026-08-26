"""카탈로그 동기화 테스트.

30분마다 도는 유일한 진입점이고, 여기서 틀리면 조용하다 -- 시트가 카탈로그에
영영 안 실리거나(커서 오전진), 멀쩡한 행이 지워진다(삭제 게이트). 사람은
"그 시트 왜 안 찾아지지" 를 겪기 전까지 모른다.
"""

from datetime import datetime, timedelta, timezone

from scripts import sync_sheet_catalog as sync

STARTED = datetime(2026, 8, 26, 3, 0, 0, tzinfo=timezone.utc)
FILES = [
    {"id": "AAA", "name": "새 응답", "modifiedTime": "2026-08-26T02:50:00.000Z"},
    {"id": "BBB", "name": "옛 응답", "modifiedTime": "2026-08-20T01:00:00.000Z"},
]
TABS = [{"id": 0, "title": "응답 시트1", "header": ["성함", "전화"]}]


def _drive(monkeypatch, files, broken=()):
    monkeypatch.setattr(sync, "list_spreadsheet_files", lambda: files)

    def headers(spreadsheet_id):
        if spreadsheet_id in broken:
            raise RuntimeError("400 range 가 깨졌습니다")
        return TABS

    monkeypatch.setattr(sync, "get_worksheet_headers", headers)


def test_커서보다_오래된_시트는_안_훑는다(monkeypatch):
    _drive(monkeypatch, FILES)

    collected, failed = sync.collect("2026-08-25T00:00:00.000Z")

    assert [item["file"]["id"] for item in collected] == ["AAA"]
    assert failed == []


def test_커서가_없으면_전량을_훑는다(monkeypatch):
    _drive(monkeypatch, FILES)

    collected, _ = sync.collect("")

    assert len(collected) == 2


def test_한_시트가_터져도_나머지는_계속한다(monkeypatch):
    # 한 시트의 탭 이름이 A1 표기를 깨는 경우가 실제로 있다. 거기서 멈추면
    # 그날 수정된 다른 시트가 전부 카탈로그에 안 들어간다.
    _drive(monkeypatch, FILES, broken={"AAA"})

    collected, failed = sync.collect("")

    assert [item["file"]["id"] for item in collected] == ["BBB"]
    assert [file["id"] for file in failed] == ["AAA"]


def test_실패가_없으면_커서를_끝까지_민다():
    assert sync.next_cursor(STARTED, []) == sync.stamp(STARTED - sync.OVERLAP)


def test_실패한_시트_앞에서_커서가_멈춘다():
    # 그 시트가 다음 실행에도 후보로 잡혀야 한다. 커서를 끝까지 밀면 다음에
    # 누가 그 시트를 수정할 때까지 영영 안 잡힌다.
    cursor = sync.next_cursor(STARTED, [FILES[0]])

    assert cursor < FILES[0]["modifiedTime"]
    assert cursor == "2026-08-26T02:49:59.000Z"


def test_커서는_실패가_있어도_전진한다():
    # 못 고치는 시트 하나가 커서를 영영 붙잡으면 드라이브 전체가 멈춘다.
    old = dict(FILES[1])
    stuck = sync.next_cursor(STARTED, [old])

    assert stuck > "2026-08-19T00:00:00.000Z"
    assert stuck < old["modifiedTime"]


def test_시각을_Drive와_같은_모양으로_찍는다():
    # 커서와 modifiedTime 을 문자열로 비교한다. 자릿수가 다르면 비교가 어긋난다.
    moment = STARTED + timedelta(microseconds=123456)

    assert sync.stamp(moment) == "2026-08-26T03:00:00.123Z"
    assert len(sync.stamp(STARTED)) == len(FILES[0]["modifiedTime"])
