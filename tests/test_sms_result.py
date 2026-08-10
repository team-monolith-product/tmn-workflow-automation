"""
도달 결과 파싱·기록 테스트
"""

import pytest

from service.sms import result as sms_result


def test_parse_results_reads_phone_and_status_per_row():
    """행마다 번호와 상태를 뽑아 성공/실패로 접습니다."""
    html = """
    <table>
      <tr><th>수신번호</th><th>결과</th></tr>
      <tr><td>010-1111-2222</td><td>성공</td></tr>
      <tr><td>01033334444</td><td>발송실패</td></tr>
    </table>
    """
    assert sms_result.parse_results(html) == {
        "01011112222": sms_result.DELIVERED,
        "01033334444": sms_result.FAILED,
    }


def test_parse_results_skips_unresolved_rows():
    """아직 전송 중인 행은 결과로 치지 않습니다. 미확정을 실패로 세면 재발송이 중복 발송이 됩니다."""
    html = "<table><tr><td>010-5555-6666</td><td>전송중</td></tr></table>"
    assert sms_result.parse_results(html) == {}


def test_parse_results_keeps_newest_row_per_phone():
    """같은 번호가 여러 번 나오면 위쪽(최신) 행을 남깁니다."""
    html = """
    <table>
      <tr><td>010-1111-2222</td><td>성공</td></tr>
      <tr><td>010-1111-2222</td><td>실패</td></tr>
    </table>
    """
    assert sms_result.parse_results(html) == {"01011112222": sms_result.DELIVERED}


class FakeCursor:
    """execute 로 들어온 SQL 과 파라미터를 기록하는 커서"""

    def __init__(self, failed_rows):
        self.calls = []
        self._failed_rows = failed_rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.calls.append((sql.split()[0], params))

    def fetchall(self):
        return self._failed_rows


class FakeConn:
    def __init__(self, failed_rows):
        self.cursor_obj = FakeCursor(failed_rows)
        self.committed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1


def test_record_writes_each_status_and_returns_failures():
    """확정된 결과를 행마다 기록하고, 그 캠페인의 실패 건을 재발송 대상으로 돌려줍니다."""
    conn = FakeConn([{"phone": "01033334444", "name": "나"}])

    failed = sms_result.record(
        conn,
        "discord",
        {"01011112222": sms_result.DELIVERED, "01033334444": sms_result.FAILED},
    )

    updates = [params for verb, params in conn.cursor_obj.calls if verb == "UPDATE"]
    assert len(updates) == 2
    assert {update["phone"] for update in updates} == {"01011112222", "01033334444"}
    assert failed == [{"to": "01033334444", "name": "나"}]
    assert conn.committed == 1


def test_record_with_no_statuses_still_reports_failures():
    """이번 폴링에서 새로 확정된 게 없어도 기존 실패 건은 그대로 나옵니다."""
    conn = FakeConn([])
    assert sms_result.record(conn, "discord", {}) == []
