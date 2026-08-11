"""채널-참가자시트 연결 테스트.

미연결 채널은 이 기능의 첫 사용이 반드시 밟는 경로다. 여기가 깨지면 신규
채널에서 문자를 처음 보내려는 사람은 안내 문구 대신 내부 오류를 본다.
이 파일이 한때 내용 없는 껍데기였고, 그래서 NotConnected 가 통째로
사라진 것을 322개 테스트가 못 잡았다.
"""

from unittest.mock import MagicMock

import pytest

from app import sms as sms_tools
from service.sms import roster


class _ctx:
    """connect() 가 돌려주는 컨텍스트 매니저를 흉내냅니다."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_):
        return False


def _returning(monkeypatch, row):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = row
    monkeypatch.setattr(roster, "connect", lambda: _ctx(conn))
    return conn


def test_연결이_없으면_NotConnected를_던진다(monkeypatch):
    _returning(monkeypatch, None)

    with pytest.raises(roster.NotConnected):
        roster.sheet_for("C1")


def test_연결이_있으면_시트_ID를_돌려준다(monkeypatch):
    _returning(monkeypatch, {"spreadsheet_id": "S1"})

    assert roster.sheet_for("C1") == "S1"


def test_호출부가_잡을_수_있는_이름이다():
    # `except roster.NotConnected` 가 성립하려면 모듈에 그 이름이 있어야 한다.
    # 없으면 AttributeError 로 바뀌어 도구가 내부 오류로 터진다.
    assert issubclass(roster.NotConnected, Exception)
    assert hasattr(sms_tools.roster, "NotConnected")
