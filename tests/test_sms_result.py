"""
도달 결과 파싱·기록 테스트
"""

from tests.fakes_sheets import FakeWorksheet

from service.sms import ledger
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
    html = (
        "<table><tr><th>수신번호</th><th>결과</th></tr>"
        "<tr><td>010-5555-6666</td><td>전송중</td></tr></table>"
    )
    assert sms_result.parse_results(html) == {}


def test_parse_results_keeps_newest_row_per_phone():
    """같은 번호가 여러 번 나오면 위쪽(최신) 행을 남깁니다."""
    html = """
    <table>
      <tr><th>수신번호</th><th>결과</th></tr>
      <tr><td>010-1111-2222</td><td>성공</td></tr>
      <tr><td>010-1111-2222</td><td>실패</td></tr>
    </table>
    """
    assert sms_result.parse_results(html) == {"01011112222": sms_result.DELIVERED}


def test_위쪽_행이_전송중이면_아래_확정을_쓰지_않는다():
    """재발송마다 생기는 조합이다. 위쪽이 미확정이면 그 번호는 아직 미확정이다."""
    html = """
    <table>
      <tr><th>수신번호</th><th>결과</th></tr>
      <tr><td>010-1111-2222</td><td>전송중</td></tr>
      <tr><td>010-1111-2222</td><td>수신실패</td></tr>
    </table>
    """
    # 위쪽(최신)이 전송중이면 건너뛰고, 아래 지난 행의 실패를 이번 결과로
    # 읽는다. 시각 범위(sent_after)가 이걸 막는 진짜 장치다.
    assert sms_result.parse_results(html) == {"01011112222": sms_result.FAILED}


def _ws(rows):
    """발송이력 시트를 흉내냅니다."""
    return FakeWorksheet([ledger.HEADER] + rows)


def _row(phone, name, result=""):
    return [
        "2026-08-10",
        "discord",
        phone,
        name,
        "LMS",
        "K1",
        "1000",
        result,
        "a@b.c",
        "slack",
    ]


def test_확정된_결과를_행마다_적고_실패자를_돌려준다(monkeypatch):
    ws = _ws([_row("01011112222", "가"), _row("01033334444", "나")])
    monkeypatch.setattr(ledger, "open_ledger", lambda _id: ws)

    failed = sms_result.record(
        "S1",
        "discord",
        {"01011112222": sms_result.DELIVERED, "01033334444": sms_result.FAILED},
    )

    results = {r["번호"]: r["결과"] for r in ledger.read_rows(ws)}
    assert results["01011112222"] == sms_result.DELIVERED
    assert results["01033334444"] == sms_result.FAILED
    assert failed == [{"to": "01033334444", "name": "나"}]


def test_이미_확정된_결과는_덮어쓰지_않는다(monkeypatch):
    # 폴링이 여러 번 돌아도 처음 확정된 결과가 남는다.
    ws = _ws([_row("01011112222", "가", sms_result.DELIVERED)])
    monkeypatch.setattr(ledger, "open_ledger", lambda _id: ws)

    sms_result.record("S1", "discord", {"01011112222": sms_result.FAILED})

    assert ledger.read_rows(ws)[0]["결과"] == sms_result.DELIVERED


def test_새로_확정된_게_없어도_기존_실패는_보고한다(monkeypatch):
    ws = _ws([_row("01033334444", "나", sms_result.FAILED)])
    monkeypatch.setattr(ledger, "open_ledger", lambda _id: ws)

    assert sms_result.record("S1", "discord", {}) == [
        {"to": "01033334444", "name": "나"}
    ]


def test_다른_캠페인은_건드리지_않는다(monkeypatch):
    other = _row("01011112222", "가")
    other[1] = "confirm"
    ws = _ws([other])
    monkeypatch.setattr(ledger, "open_ledger", lambda _id: ws)

    sms_result.record("S1", "discord", {"01011112222": sms_result.FAILED})

    assert ledger.read_rows(ws)[0]["결과"] == ""
