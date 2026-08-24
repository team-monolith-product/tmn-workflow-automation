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
            "ref_key": "R1",
            "channel_id": "C0AP8CG1Y6N",
            "thread_ts": "111.000",
            "sender": "01077647538",
            "content": "[*이름*]선생님",
            "message_type": "SMS",
            "message_key": "K1",
            "send_time": None,
            "approved_by": "U1",
            "targets": [{"to": "01011111111", "name": "홍길동"}],
            **extra,
        }
    )


def _rows(cur):
    return [params for _, params in cur.calls]


def test_발송을_남긴다(cursor):
    _record()

    row = _rows(cursor)[0]
    assert row["ref_key"] == "R1"
    assert row["channel_id"] == "C0AP8CG1Y6N"
    assert row["thread_ts"] == "111.000"
    assert row["message_type"] == "SMS"
    assert row["approved_by"] == "U1"


def test_발신번호를_남긴다(cursor):
    # 발신번호가 둘 이상이 되면 "어느 번호로 나갔나" 에 답할 수 있어야 한다.
    # 회신 전화가 어디로 갈지도 이 번호가 정한다.
    _record(sender="01027055324")

    assert _rows(cursor)[0]["sender"] == "01027055324"


def test_치환_전_원문을_남긴다(cursor):
    # 치환 후 문장을 남기면 재사용할 때 다시 템플릿으로 되돌려야 하고,
    # 그 과정에서 담당자 이름까지 태그로 바뀌는 사고가 난다.
    _record()

    assert _rows(cursor)[0]["content"] == "[*이름*]선생님"


def test_접수키가_없어도_남긴다(cursor):
    # 벤더가 code 1000 을 주면서 messageKey 를 빠뜨릴 수 있다. 그때 이력이
    # 통째로 안 남으면 문안과 수신자까지 같이 잃는다.
    _record(message_key=None)

    assert _rows(cursor)[0]["message_key"] is None


def test_받는_사람마다_한_행이다(cursor):
    _record(
        targets=[
            {"to": "01011111111", "name": "가", "changeWord": {"var1": "1기"}},
            {"to": "01022222222", "name": "나"},
        ]
    )

    rows = _rows(cursor)
    assert [row["phone"] for row in rows] == ["01011111111", "01022222222"]
    assert [row["name"] for row in rows] == ["가", "나"]
    assert json.loads(rows[0]["change_word"]) == {"var1": "1기"}
    assert rows[1]["change_word"] is None


def test_한_발송의_모든_행이_같은_ref_key다(cursor):
    # 발송 건수를 count(distinct ref_key) 로 세므로, 행마다 값이 달라지면
    # 한 번 보낸 문자가 148건으로 잡힌다.
    _record(
        targets=[
            {"to": "01011111111", "name": "가"},
            {"to": "01022222222", "name": "나"},
            {"to": "01033333333", "name": "다"},
        ]
    )

    rows = _rows(cursor)
    assert len(rows) == 3
    assert {row["ref_key"] for row in rows} == {"R1"}
    # 문안·발신번호·시각도 같은 발송이면 같아야 한다.
    assert len({row["content"] for row in rows}) == 1
    assert len({row["sender"] for row in rows}) == 1


def test_사업을_발송_시점에_박는다(cursor):
    # 매핑 표를 조인하지 않는다. 조인은 에이전트가 SQL 을 짤 때 틀릴 자리다.
    _record()

    assert _rows(cursor)[0]["project"] == "26기업연계정보교원연수"


def test_매핑에_없는_채널이면_사업이_비어_있다(cursor):
    # NULL 로 남겨 두면 나중에 UPDATE 로 소급할 수 있다. 빈 문자열로 채우면
    # "매핑이 없다" 와 "사업이 없다" 가 구분되지 않는다.
    _record(channel_id="C9999")

    assert _rows(cursor)[0]["project"] is None


def test_예약_시각을_남긴다(cursor):
    # 벤더 형식은 시간대가 없는 KST 문자열이라, 그대로 넣으면 서버 시간대로
    # 읽혀 9시간이 밀린다.
    _record(send_time="2026-08-22T09:00:00")

    assert _rows(cursor)[0]["scheduled_at"].isoformat() == "2026-08-22T09:00:00+09:00"


def test_즉시_발송이면_예약_시각이_없다(cursor):
    _record()

    assert _rows(cursor)[0]["scheduled_at"] is None
