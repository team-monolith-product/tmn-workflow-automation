"""발송 이력 저장 테스트.

행을 만드는 것과 넣는 것을 갈라 봅니다. FakeCursor 는 바인딩을 하지 않으므로
플레이스홀더와 파라미터 키가 어긋나도 잡지 못합니다. 그 어긋남은 열 목록을
한 곳에서 만들어 구조로 막고, 여기서는 그게 실제로 한 곳인지만 봅니다.
"""

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from service.sms import history
from service.sms.send import Sent

MIGRATION = "migrations/knowledge/004_sms.sql"
KST = timezone(timedelta(hours=9))
SENT = Sent(
    ref_key="R1",
    sender="01077647538",
    message_key="K1",
    message_type="SMS",
    send_at=None,
    content="[*이름*]선생님",
    targets=[{"to": "01011111111", "name": "홍길동"}],
)


def _rows(**extra):
    """기본 발송에 extra 를 덮어써서 행을 만든다."""
    channel_id = extra.pop("channel_id", "C0AP8CG1Y6N")
    project = extra.pop("project", "26기업연계정보교원연수")
    return history.build_rows(
        SENT._replace(**extra),
        channel_id=channel_id,
        project=project,
        thread_ts="111.000",
        approved_by="U1",
    )


class FakeCursor:
    """실행된 SQL과 파라미터를 순서대로 기록한다."""

    def __init__(self):
        self.calls = []

    def executemany(self, sql, seq):
        for params in seq:
            self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def cursor(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(history, "connect", lambda: FakeConn(cur))
    return cur


def test_열_목록이_마이그레이션과_같다():
    # 행 모양에 필드를 더하고 SQL 을 안 고치면 INSERT 가 런타임에 터진다.
    # 하필 문자가 이미 나간 직후라, 여기서 잡아야 한다.
    sql = (Path(__file__).parent.parent / MIGRATION).read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s{4}(\w+)\s", sql, re.M))

    assert set(history.SMS_LOG_COLUMNS) <= declared


def test_설정에_없는_채널이면_사업이_비어_있다(cursor, monkeypatch):
    # record 가 설정을 읽는 유일한 자리다. build_rows 는 받은 값을 쓸 뿐이다.
    monkeypatch.setattr(
        history, "load_config", lambda: SimpleNamespace(sms_projects={})
    )

    history.record(SENT, channel_id="C9999", thread_ts="111.000", approved_by="U1")

    _, params = cursor.calls[0]
    assert params["project"] is None


def test_받는_사람마다_한_행이다():
    rows = _rows(
        targets=[
            {"to": "01011111111", "name": "가", "changeWord": {"var1": "1기"}},
            {"to": "01022222222", "name": "나"},
        ]
    )

    assert [row.phone for row in rows] == ["01011111111", "01022222222"]
    assert [row.name for row in rows] == ["가", "나"]
    assert json.loads(rows[0].change_word) == {"var1": "1기"}
    assert rows[1].change_word is None


def test_한_발송의_모든_행이_같은_ref_key다():
    # 발송 건수를 count(distinct ref_key) 로 세므로, 행마다 값이 달라지면
    # 한 번 보낸 문자가 148건으로 잡힌다.
    rows = _rows(
        targets=[
            {"to": "01011111111", "name": "가"},
            {"to": "01022222222", "name": "나"},
            {"to": "01033333333", "name": "다"},
        ]
    )

    assert len(rows) == 3
    assert {row.ref_key for row in rows} == {"R1"}
    assert len({row.content for row in rows}) == 1
    assert len({row.sender for row in rows}) == 1


def test_사업을_발송_시점에_박는다():
    # 매핑 표를 조인하지 않는다. 조인은 에이전트가 SQL 을 짤 때 틀릴 자리다.
    # NULL 로 남겨 두면 나중에 UPDATE 로 소급할 수 있고, 빈 문자열로 채우면
    # "매핑이 없다" 와 "사업이 없다" 가 구분되지 않는다.
    assert (
        _rows(project="26기업연계정보교원연수")[0].project == "26기업연계정보교원연수"
    )
    assert _rows(project=None)[0].project is None


def test_예약_시각을_시간대와_함께_남긴다():
    # 예약 시각은 aware datetime 으로 흐른다. 문자열로 눌렀다가 되살리면
    # 시간대를 잃고 서버 시간대로 읽혀 9시간이 밀린다.
    when = datetime(2026, 8, 22, 9, 0, tzinfo=KST)

    rows = _rows(send_at=when)

    assert rows[0].scheduled_at.isoformat() == "2026-08-22T09:00:00+09:00"


def test_즉시_발송이면_예약_시각이_없다():
    assert _rows()[0].scheduled_at is None
