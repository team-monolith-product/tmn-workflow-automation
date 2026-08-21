"""발송 이력 저장 테스트.

FakeCursor 는 어떤 SQL 에 어떤 파라미터가 실렸는지만 봅니다. 커밋 여부는
psycopg 의 계약이지 이 코드의 성질이 아니라, 여기서 검증하지 않습니다.
"""

import json

import pytest

from service.sms import history


class FakeCursor:
    """실행된 SQL과 파라미터를 순서대로 기록한다."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def executemany(self, sql, seq):
        for params in seq:
            self.calls.append((sql, params))

    def fetchone(self):
        return {"id": 77}

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


def _record(**extra):
    history.record(
        **{
            "channel_id": "C1",
            "thread_ts": "111.000",
            "content": "[*이름*]선생님",
            "message_type": "SMS",
            "message_key": "K1",
            "approved_by": "U1",
            "targets": [{"to": "01011111111", "name": "홍길동"}],
            **extra,
        }
    )


def _params(cur, fragment):
    return next(params for sql, params in cur.calls if fragment in sql)


def test_발송을_남긴다(cursor):
    _record()

    sent = _params(cursor, "INSERT INTO sms_send")
    assert sent["channel_id"] == "C1"
    assert sent["thread_ts"] == "111.000"
    assert sent["message_type"] == "SMS"
    assert sent["approved_by"] == "U1"


def test_치환_전_원문을_남긴다(cursor):
    # 치환 후 문장을 남기면 재사용할 때 다시 템플릿으로 되돌려야 하고,
    # 그 과정에서 담당자 이름까지 태그로 바뀌는 사고가 난다.
    _record()

    assert _params(cursor, "INSERT INTO sms_send")["content"] == "[*이름*]선생님"


def test_접수키가_없어도_남긴다(cursor):
    # 벤더가 code 1000 을 주면서 messageKey 를 빠뜨릴 수 있다. 그때 이력이
    # 통째로 안 남으면 문안과 수신자까지 같이 잃는다.
    _record(message_key=None)

    assert _params(cursor, "INSERT INTO sms_send")["message_key"] is None


def test_수신자를_같이_남긴다(cursor):
    _record(
        targets=[
            {"to": "01011111111", "name": "가", "changeWord": {"var1": "1기"}},
            {"to": "01022222222", "name": "나"},
        ]
    )

    rows = [
        params for sql, params in cursor.calls if "INSERT INTO sms_recipient" in sql
    ]
    assert [row["phone"] for row in rows] == ["01011111111", "01022222222"]
    assert [row["name"] for row in rows] == ["가", "나"]
    assert [row["send_id"] for row in rows] == [77, 77]
    assert json.loads(rows[0]["change_word"]) == {"var1": "1기"}
    assert rows[1]["change_word"] is None
