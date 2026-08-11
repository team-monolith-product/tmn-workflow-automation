"""발송 이력 시트 테스트 — 행 번호로 승자를 가리는 규칙을 고정한다.

시트에는 UNIQUE 제약이 없어서 이 규칙이 유일한 중복 차단 장치다. claim 의
재조회를 지우면 같은 사람에게 두 번 발송된다. 이 파일이 그걸 막는다.
"""

import pytest

from tests.fakes_sheets import FakeWorksheet

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


def _open(sheet: FakeWorksheet, monkeypatch) -> FakeWorksheet:
    """open_ledger 를 이 페이크에 물립니다."""
    monkeypatch.setattr(ledger.google_sheets, "get_worksheet", lambda _id, _name: sheet)
    return ledger.open_ledger("S1")


def test_빈_탭이면_헤더를_쓴다(monkeypatch):
    sheet = _open(FakeWorksheet([]), monkeypatch)
    assert sheet.rows[0] == ledger.HEADER


def test_사람이_먼저_만든_탭에도_번호_열을_텍스트로_고정한다(monkeypatch):
    # 이 서식이 없으면 손으로 적은 01012345678 이 숫자가 되어 앞자리 0 을 잃고,
    # 그 줄은 어떤 발송과도 대조되지 않는다. 헤더 분기 안에 두면 사람이 탭을
    # 먼저 만들어 둔 시트에서 영영 안 잡힌다.
    sheet = _open(FakeWorksheet([ledger.HEADER]), monkeypatch)
    assert sheet.formats[f"{ledger.PHONE_COLUMN}:{ledger.PHONE_COLUMN}"] == {
        "numberFormat": {"type": "TEXT"}
    }


def test_열이_밀린_시트는_열기_전에_거절한다(monkeypatch):
    # 쓰기는 HEADER 순서를 위치로 가정한다. 앞에 열이 하나 끼면 캠페인 칸에
    # 번호가 들어가 승자 조회가 영영 매칭되지 않고, 전원이 '중복'으로 찍힌 뒤
    # 한 통도 안 나간다. 종료 코드는 0 이라 사람은 발송된 줄 안다.
    with pytest.raises(ledger.LedgerLayoutError):
        _open(FakeWorksheet([["비고"] + ledger.HEADER]), monkeypatch)


def test_뒤에_열을_덧붙인_시트는_그대로_쓴다(monkeypatch):
    # 흔한 편집이고 위치 가정을 깨뜨리지 않는다.
    sheet = _open(FakeWorksheet([ledger.HEADER + ["비고"]]), monkeypatch)
    assert sheet.rows[0] == ledger.HEADER + ["비고"]


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


def test_claim이_쓴_행이_헤더와_같은_폭이다():
    # 폭이 어긋나면 값이 옆 칸으로 밀린다. 승자 판정 열(캠페인·번호·접수코드)은
    # 앞쪽이라 중복 차단은 멀쩡한 채, 감사 기록만 조용히 틀린다.
    ws = _ws()
    ledger.claim(
        ws, "discord", [{"to": "01011111111", "name": "가"}], "LMS", "a@b.c", "slack"
    )

    written = ws.rows[1]
    assert len(written) == len(ledger.HEADER)

    row = ledger.read_rows(ws)[0]
    assert row["요청자"] == "a@b.c"
    assert row["경로"] == "slack"
    assert row["이름"] == "가"
    assert row["타입"] == "LMS"


def test_사람이_하이픈을_넣어_적어도_대조된다():
    # 장애 중 뿌리오 웹으로 보내고 손으로 적은 줄. 사람은 010-1111-1111 로
    # 적는데 우리는 01011111111 로 쓴다. 표기를 안 눕히면 대조가 안 돼
    # 그 사람에게 한 번 더 나간다.
    ws = _ws([_row("discord", "010-1111-1111", "1000")])
    won, lost = ledger.claim(
        ws, "discord", [{"to": "01011111111"}], "LMS", "a@b.c", "slack"
    )
    assert won == [] and len(lost) == 1


def test_번호의_앞자리_0을_잃지_않는다():
    # 앞자리 0 이 날아가면 아래 재조회가 우리가 쓴 번호를 못 찾아 전원이
    # 지고, 그 캠페인은 한 통도 나가지 않는다.
    ws = _ws()
    ledger.claim(ws, "discord", [{"to": "01011111111"}], "LMS", "a@b.c", "slack")
    assert ledger.read_rows(ws)[0]["번호"] == "01011111111"


def test_USER_ENTERED로_쓰면_앞자리_0이_사라진다():
    # 페이크가 시트 동작을 흉내내는지 자체를 고정한다. 이게 무너지면 위
    # 테스트가 아무것도 검증하지 못한다.
    ws = FakeWorksheet([])
    ws.append_rows([["01011111111"]], value_input_option="USER_ENTERED")
    assert ws.rows[0][0] == "1011111111"


def test_같은_번호를_두_번_넘기면_거절한다():
    # 번호 -> 행 매핑이 덮여 승자 행의 주인이 사라진다. 호출부가 접어야 한다.
    with pytest.raises(ValueError, match="같은 번호"):
        ledger.claim(
            _ws(), "discord", [{"to": "010"}, {"to": "010"}], "LMS", "a@b.c", "slack"
        )
