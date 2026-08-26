"""카탈로그 동기화 테스트.

30분마다 도는 유일한 진입점이고, 여기서 틀리면 조용하다 -- 시트가 카탈로그에
영영 안 실리거나(재시도 누락), 멀쩡한 행이 지워진다(삭제 게이트). 사람은
"그 시트 왜 안 찾아지지" 를 겪기 전까지 모른다.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from scripts import sync_sheet_catalog as sync
from service.sheets import catalog

FILES = [
    {
        "id": "AAA",
        "name": "새 응답",
        "modifiedTime": "2026-08-26T02:50:00.000Z",
        "webViewLink": "https://docs.google.com/spreadsheets/d/AAA/edit",
    },
    {
        "id": "BBB",
        "name": "옛 응답",
        "modifiedTime": "2026-08-20T01:00:00.000Z",
        "webViewLink": "https://docs.google.com/spreadsheets/d/BBB/edit",
    },
]
TABS = [{"id": 0, "title": "응답 시트1", "header": ["성함", "전화"]}]


def _drive(monkeypatch, files, broken=()):
    monkeypatch.setattr(sync, "PACE_SECONDS", 0)
    monkeypatch.setattr(sync, "list_spreadsheet_files", lambda: files)

    def headers(spreadsheet_id):
        if spreadsheet_id in broken:
            raise RuntimeError("400 range 가 깨졌습니다")
        return TABS

    monkeypatch.setattr(sync, "get_worksheet_headers", headers)


class FakeConn:
    """execute 를 기록만 하는 커넥션. 삭제가 도는지 보려고 쓴다."""

    def __init__(self, known=()):
        self.known = list(known)
        self.calls: list[tuple[str, dict]] = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if sql is sync.READ_KNOWN:
            return iter(self.known)
        return self

    def deleted(self):
        """삭제 쿼리가 돌았으면 그 alive 목록, 아니면 None."""
        for sql, params in self.calls:
            if sql is sync.DELETE_MISSING:
                return params["alive"]
        return None


def _db(monkeypatch, conn):
    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(sync, "connect", connect)
    monkeypatch.setattr(sync, "upsert_source", lambda *a, **k: 1)
    monkeypatch.setattr(sync, "upsert_item", lambda c, row: {"inserted": True})


def test_안_바뀐_시트는_머리행을_안_읽는다(monkeypatch):
    # 커서를 없앤 자리를 이것이 대신한다. 안 되면 30분마다 전량을 다시 읽는다.
    _drive(monkeypatch, FILES)
    known = {"AAA": catalog.modified_at(FILES[0])}

    collected, failed, skipped = sync.collect(FILES, known)

    assert [item["file"]["id"] for item in collected] == ["BBB"]
    assert skipped == 1
    assert failed == []


def test_수정되면_다시_읽는다(monkeypatch):
    _drive(monkeypatch, FILES)
    stale = {"AAA": datetime(2020, 1, 1, tzinfo=timezone.utc)}

    collected, _, skipped = sync.collect(FILES, stale)

    assert [item["file"]["id"] for item in collected] == ["AAA", "BBB"]
    assert skipped == 0


def test_한_시트가_터져도_나머지는_계속한다(monkeypatch):
    # 한 시트의 탭 이름이 A1 표기를 깨는 경우가 실제로 있다. 거기서 멈추면
    # 그날 수정된 다른 시트가 전부 카탈로그에 안 들어간다.
    _drive(monkeypatch, FILES, broken={"AAA"})

    collected, failed, _ = sync.collect(FILES, {})

    assert [item["file"]["id"] for item in collected] == ["BBB"]
    assert [file["id"] for file in failed] == ["AAA"]


def test_실패한_시트는_다음_실행에_다시_후보가_된다(monkeypatch):
    # 재시도 장치가 따로 없다. 저장값이 안 갱신되는 것이 곧 재시도다.
    # 이것이 깨지면 못 고치는 시트 하나가 영영 카탈로그에 안 들어간다.
    _drive(monkeypatch, FILES, broken={"AAA"})
    known = {"BBB": catalog.modified_at(FILES[1])}  # AAA 는 실패했으니 저장된 적 없다

    collected, failed, skipped = sync.collect(FILES, known)

    assert [file["id"] for file in failed] == ["AAA"]
    assert collected == [] and skipped == 1


def test_modifiedTime_이_없으면_터진다():
    # 기본값을 두면 모든 시트가 "안 바뀜" 이 되어 카탈로그가 조용히 안 갱신된다.
    with pytest.raises(KeyError):
        catalog.modified_at({"id": "X", "name": "이름만"})


def test_사라진_시트를_지운다(monkeypatch):
    # 휴지통에 갔거나 공유가 끊긴 시트를 남겨 두면 query_knowledge 가 계속
    # 찾아 주고, 그다음 읽기가 권한 오류로 터진다.
    _drive(monkeypatch, FILES)
    conn = FakeConn()
    _db(monkeypatch, conn)

    sync.main()

    assert conn.deleted() == ["AAA", "BBB"]


def test_안_바뀌거나_실패한_시트도_살아_있는_것으로_센다(monkeypatch):
    # alive 가 "이번에 훑은 것" 이면 건너뛴 시트와 실패한 시트가 지워진다.
    _drive(monkeypatch, FILES, broken={"AAA"})
    conn = FakeConn(
        known=[
            {"external_id": "BBB", "source_updated_at": catalog.modified_at(FILES[1])}
        ]
    )
    _db(monkeypatch, conn)

    sync.main()

    # AAA 는 실패, BBB 는 안 바뀌어 건너뜀. 적재한 것이 없어도 둘 다 살아 있다.
    assert conn.deleted() == ["AAA", "BBB"]


def test_목록이_비면_지우지_않는다(monkeypatch):
    # 공유 해제나 스코프 회귀로 0건이 오면 카탈로그가 통째로 날아간다.
    _drive(monkeypatch, [])
    conn = FakeConn()
    _db(monkeypatch, conn)

    sync.main()

    assert conn.deleted() is None
