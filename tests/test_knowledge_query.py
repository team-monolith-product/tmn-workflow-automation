"""지식베이스 읽기 전용 SQL 실행 테스트"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import psycopg

from service.knowledge.query import (
    MAX_CHAR_LIMIT,
    format_value,
    render_rows,
    run_query,
)


class FakeCursor:
    """execute를 기록하고 주어진 행을 돌려주는 커서"""

    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.executed = []
        self.itersize = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.error:
            raise self.error

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    """cursor()를 부를 때마다 같은 커서를 돌려주는 커넥션"""

    def __init__(self, cursor):
        self.cursor_kwargs = None
        self._cursor = cursor

    def cursor(self, **kwargs):
        self.cursor_kwargs = kwargs
        return self._cursor


@contextmanager
def _patched_connect(read_cursor, log_cursor, read_only_calls):
    """읽기용과 기록용 커넥션을 갈라 주고 read_only 인자를 모읍니다."""

    @contextmanager
    def fake_connect(dsn=None, read_only=None):
        read_only_calls.append(read_only)
        yield FakeConnection(read_cursor if read_only else log_cursor)

    with patch("service.knowledge.query.connect", fake_connect):
        yield


def _logged_filters(log_cursor):
    """기록된 query_log 행에서 filters를 꺼냅니다."""
    return json.loads(log_cursor.executed[0][1]["filters"])


def test_NULL은_빈_칸이다():
    assert format_value(None) == ""


def test_jsonb는_한글을_그대로_쓴다():
    assert format_value({"text": "배포"}) == '{"text": "배포"}'


def test_시각은_ISO로_적는다():
    value = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)
    assert format_value(value) == "2026-08-06T09:30:00+00:00"


def test_줄바꿈은_공백으로_바꾼다():
    # 한 행이 한 줄이어야 표로 읽힌다.
    assert format_value("첫 줄\n둘째 줄") == "첫 줄 둘째 줄"


def test_결과가_없으면_없다고_말한다():
    rendered, row_count, truncated = render_rows([], 100)

    assert rendered == "결과가 없습니다."
    assert (row_count, truncated) == (0, False)


def test_첫_행의_키가_머리글이_된다():
    rendered, row_count, truncated = render_rows(
        [{"channel": "t_개발_백", "title": "배포 실패"}], 100
    )

    assert rendered == "channel | title\nt_개발_백 | 배포 실패"
    assert (row_count, truncated) == (1, False)


def test_글자_상한을_넘기면_자르고_알린다():
    rendered, row_count, truncated = render_rows(
        [{"raw_text": "가" * 100} for _ in range(10)], 120
    )

    assert len(rendered.split("\n…")[0]) == 120
    assert "120자에서 잘렸습니다" in rendered
    assert (row_count, truncated) == (2, True)


def test_상한을_넘긴_뒤로는_행을_당겨오지_않는다():
    # 호출부가 서버 커서를 넘기므로, 여기서 멈추면 남은 행은 서버에 남는다.
    pulled = []

    def rows():
        for index in range(10):
            pulled.append(index)
            yield {"raw_text": "가" * 100}

    render_rows(rows(), 120)

    assert pulled == [0, 1]


async def test_질의는_읽기_전용_커넥션에서_돈다():
    read_cursor = FakeCursor(rows=[{"id": 1}])
    log_cursor = FakeCursor()
    read_only_calls = []

    with _patched_connect(read_cursor, log_cursor, read_only_calls):
        rendered = run_query(
            "SELECT id FROM item", actor="lch@team-mono.com", tool="mcp"
        )

    assert rendered == "id\n1"
    assert read_cursor.executed[0][0] == "SELECT id FROM item"
    # 기록은 쓰기라서 읽기 전용 커넥션으로는 남길 수 없다.
    assert read_only_calls == [True, None]


async def test_질의를_기록한다():
    log_cursor = FakeCursor()

    with _patched_connect(FakeCursor(rows=[{"id": 1}]), log_cursor, []):
        run_query("SELECT id FROM item", actor="lch@team-mono.com", tool="slack")

    logged = log_cursor.executed[0][1]
    assert logged["actor"] == "lch@team-mono.com"
    assert logged["tool"] == "slack"
    assert logged["query"] == "SELECT id FROM item"
    assert _logged_filters(log_cursor)["rows"] == 1


async def test_글자_상한은_MAX를_넘지_못한다():
    log_cursor = FakeCursor()

    with _patched_connect(FakeCursor(), log_cursor, []):
        run_query("SELECT 1", actor="lch@team-mono.com", tool="mcp", char_limit=10**9)

    assert _logged_filters(log_cursor)["char_limit"] == MAX_CHAR_LIMIT


async def test_실패한_질의는_이유를_돌려주고_기록에_남는다():
    # 에이전트가 SQL을 고쳐 다시 부를 수 있어야 하므로 예외로 올리지 않는다.
    error = psycopg.errors.SyntaxError("syntax error at or near FROM")
    log_cursor = FakeCursor()

    with _patched_connect(FakeCursor(error=error), log_cursor, []):
        rendered = run_query("SELEC 1", actor="lch@team-mono.com", tool="mcp")

    assert "질의가 실패했습니다" in rendered
    assert "syntax error" in rendered
    assert "syntax error" in _logged_filters(log_cursor)["error"]
