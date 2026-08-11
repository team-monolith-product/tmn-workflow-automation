"""발송 이력 시트 테스트 — 행 번호로 승자를 가리는 규칙을 고정한다.

시트에는 UNIQUE 제약이 없어서 이 규칙이 유일한 중복 차단 장치다. claim 의
재조회를 지우면 같은 사람에게 두 번 발송된다. 이 파일이 그걸 막는다.
"""

from tests.conftest_sms import FakeWorksheet

from service.sms import ledger


def _ws(rows: list[list] | None = None) -> FakeWorksheet:
    return FakeWorksheet([ledger.HEADER] + (rows or []))


def _row(campaign: str, phone: str, code: str = "") -> list:
    return ["2026-08-06", campaign, phone, "가", "LMS", "", code, "", "a@b.c", "slack"]


def test_행_번호와_함께_읽는다():
    ws = _ws([_row("discord", "01011111111")])
    rows = ledger.read_rows(ws)
    assert rows[0]["_row"] == 2
    assert rows[0]["캠페인"] == "discord"


def test_열_순서가_바뀌어도_이름으로_찾는다():
    # 사람이 시트를 편집하다 열을 옮길 수 있다.
    header = ["번호", "캠페인", "일시", "접수코드"]
    ws = FakeWorksheet([header, ["01011111111", "discord", "2026-08-06", "1000"]])
    rows = ledger.read_rows(ws)
    assert rows[0]["캠페인"] == "discord" and rows[0]["접수코드"] == "1000"


def test_같은_대상은_가장_위_행이_이긴다():
    ws = _ws([_row("discord", "010"), _row("discord", "010")])
    assert ledger.owners(ledger.read_rows(ws))[("discord", "010")] == 2


def test_실패와_중복은_죽은_것으로_본다():
    # 실패한 발송을 다시 시도할 수 있어야 한다.
    ws = _ws(
        [
            _row("discord", "010", "실패"),
            _row("discord", "010", "중복"),
            _row("discord", "010"),
        ]
    )
    assert ledger.owners(ledger.read_rows(ws))[("discord", "010")] == 4


def test_빈_접수코드는_살아_있는_것으로_본다():
    # "보냈는지 모름"이라 사람이 확인해야 한다. 조용히 다시 보내지 않는다.
    ws = _ws([_row("discord", "010", "")])
    assert ("discord", "010") in ledger.owners(ledger.read_rows(ws))


def test_캠페인이_다르면_별개다():
    ws = _ws([_row("discord", "010"), _row("confirm", "010")])
    winner = ledger.owners(ledger.read_rows(ws))
    assert winner[("discord", "010")] == 2 and winner[("confirm", "010")] == 3


def test_처음_잡으면_전원_이긴다():
    ws = _ws()
    won, lost = ledger.claim(
        ws, "discord", [{"to": "010"}, {"to": "011"}], "LMS", "a@b.c", "slack"
    )
    assert [w["to"] for w in won] == ["010", "011"] and lost == []


def test_이미_잡힌_대상은_진다():
    ws = _ws([_row("discord", "010")])
    won, lost = ledger.claim(
        ws, "discord", [{"to": "010"}, {"to": "011"}], "LMS", "a@b.c", "slack"
    )
    assert [w["to"] for w in won] == ["011"]
    assert lost == [3]  # 010 을 위해 덧붙인 행


def test_동시_클레임은_한_쪽만_이긴다():
    # 두 실행이 같은 대상을 잡으면 먼저 append 한 쪽만 발송한다.
    ws = _ws()
    first, _ = ledger.claim(ws, "discord", [{"to": "010"}], "LMS", "a@b.c", "slack")
    second, lost = ledger.claim(ws, "discord", [{"to": "010"}], "LMS", "x@y.z", "mcp")
    assert len(first) == 1 and second == [] and len(lost) == 1


def test_표시하면_접수코드와_키가_들어간다():
    ws = _ws([_row("discord", "010")])
    ledger.mark(ws, [2], "1000", "KEY1")
    row = ledger.read_rows(ws)[0]
    assert row["접수코드"] == "1000" and row["messageKey"] == "KEY1"


def test_현황을_센다():
    ws = _ws(
        [
            _row("discord", "010", "1000"),
            _row("discord", "011", "실패"),
            _row("discord", "012", "중복"),
            _row("discord", "013", ""),
            _row("confirm", "014", "1000"),
        ]
    )
    assert ledger.summarize(ledger.read_rows(ws), "discord") == {
        "total": 4,
        "accepted": 1,
        "unknown": 1,
        "duplicate": 1,
        "failed": 1,
    }


def test_같은_번호를_두_번_넘기면_거절한다():
    # 번호 -> 행 매핑이 덮여 승자 행의 주인이 사라진다. 호출부가 접어야 한다.
    import pytest

    with pytest.raises(ValueError, match="같은 번호"):
        ledger.claim(
            _ws(), "discord", [{"to": "010"}, {"to": "010"}], "LMS", "a@b.c", "slack"
        )
