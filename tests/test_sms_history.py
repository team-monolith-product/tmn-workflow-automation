"""발송 이력 저장 테스트."""

import json

import pytest

from service.sms import history


class FakeCursor:
    """실행된 SQL과 파라미터를 순서대로 기록한다."""

    def __init__(self, root_row):
        self.calls = []
        self._root_row = root_row
        self._next = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "SELECT root_ts" in sql:
            self._next = self._root_row
        elif "INSERT INTO sms_send" in sql:
            self._next = {"id": 77}
        else:
            self._next = None

    def fetchone(self):
        return self._next

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
    def make(root_row=None):
        cur = FakeCursor(root_row)
        monkeypatch.setattr(history, "connect", lambda: FakeConn(cur))
        return cur

    return make


def _record(**extra):
    return history.record(
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


def test_첫_발송은_자기_자신이_캠페인_뿌리다(cursor):
    cur = cursor()

    _record()

    assert _params(cur, "INSERT INTO sms_send")["root_ts"] == "111.000"


def test_같은_스레드의_재발송은_첫_발송을_가리킨다(cursor):
    # 누락자에게 다시 보낼 때 같은 캠페인으로 묶여야 "누가 안 받았나" 가 나온다.
    cur = cursor({"root_ts": "999.000"})

    _record()

    assert _params(cur, "INSERT INTO sms_send")["root_ts"] == "999.000"


def test_치환_전_원문을_남긴다(cursor):
    # 치환 후 문장을 남기면 재사용할 때 다시 템플릿으로 되돌려야 하고,
    # 그 과정에서 담당자 이름까지 태그로 바뀌는 사고가 난다.
    cur = cursor()

    _record()

    assert _params(cur, "INSERT INTO sms_send")["content"] == "[*이름*]선생님"


def test_수신자를_같이_남긴다(cursor):
    cur = cursor()

    _record(
        targets=[
            {"to": "01011111111", "name": "가", "changeWord": {"var1": "1기"}},
            {"to": "01022222222", "name": "나"},
        ]
    )

    rows = [params for sql, params in cur.calls if "INSERT INTO sms_recipient" in sql]
    assert [row["phone"] for row in rows] == ["01011111111", "01022222222"]
    assert [row["send_id"] for row in rows] == [77, 77]
    assert json.loads(rows[0]["change_word"]) == {"var1": "1기"}
    assert rows[1]["change_word"] is None


def test_발송_id를_돌려준다(cursor):
    cursor()

    assert _record() == 77
