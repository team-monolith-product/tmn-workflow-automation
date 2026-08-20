"""명단 컬럼 기록 테스트 — 빈 칸인 사람만 보낸다는 규칙을 고정한다.

시트에는 UNIQUE 제약도 조건부 쓰기도 없다. "그 열이 비어 있으면 아직 안 보낸
사람"이 유일한 중복 차단 장치이고, 벤더를 부르기 전에 칸을 선점하는 것이
동시 실행을 늦추는 전부다.
"""

import pytest

from tests.fakes_sheets import FakeWorksheet

from service.sms import ledger

HEADER = ["연번", "성명", "휴대폰", "소속학교"]


def _ws(rows: list[list] | None = None, header: list[str] | None = None):
    return FakeWorksheet([header or HEADER] + (rows or []))


def _person(name: str, phone: str) -> list:
    return ["1", name, phone, "OO중학교"]


def _column(ws, name: str) -> int:
    return ws.rows[0].index(name)


def test_번호_열을_이름으로_찾는다():
    header, at, by_phone = ledger._parse_roster(
        [HEADER, _person("홍길동", "010-1111-1111")]
    )

    assert header == HEADER
    assert at == 2
    assert by_phone == {"01011111111": [2]}


def test_연락처든_휴대전화든_찾는다():
    _, _, by_phone = ledger._parse_roster(
        [["연번", "성명", "연락처"], ["1", "홍길동", "010-1111-1111"]]
    )

    assert by_phone == {"01011111111": [2]}


def test_연번이_번호로_잡히지_않는다():
    # 이름 후보를 셀보다 바깥에서 돌지 않으면 '번호'가 연번에 걸려
    # 모든 수신자가 엉뚱한 값으로 대조된다.
    _, _, by_phone = ledger._parse_roster(
        [["연번", "성명", "휴대폰"], ["1", "홍길동", "010-1111-1111"]]
    )

    assert by_phone == {"01011111111": [2]}


def test_대표전화가_휴대전화보다_먼저_잡히지_않는다():
    # 후보 순서가 '전화번호' 먼저면 학교 대표전화를 수신자 번호로 읽어
    # 명단 전체가 대조에 실패하고 한 통도 안 나간다.
    _, _, by_phone = ledger._parse_roster(
        [
            ["성명", "학교 대표 전화번호", "휴대전화 번호"],
            ["홍길동", "02-123-4567", "010-1111-1111"],
        ]
    )

    assert by_phone == {"01011111111": [2]}


def test_번호_열이_없으면_거절한다():
    # 조용히 빈 명단으로 넘어가면 아무에게도 안 보내고 성공으로 끝난다.
    with pytest.raises(ledger.RosterLayoutError):
        ledger._parse_roster([["연번", "성명", "소속"]])


def test_캠페인_열이_없으면_맨_뒤에_만든다():
    # 중간에 끼우면 사람이 만든 수식과 조건부 서식이 밀린다.
    ws = _ws([_person("홍길동", "01011111111")])

    ledger.claim(ws, "discord", [{"to": "01011111111"}])

    assert ws.rows[0] == HEADER + ["discord"]


def test_같은_캠페인_열을_다시_만들지_않는다():
    ws = _ws([_person("홍길동", "01011111111")], header=HEADER + ["discord"])

    ledger.claim(ws, "discord", [{"to": "01011111111"}])

    assert len(ws.rows[0]) == 5


def test_캠페인_이름의_공백은_무시한다():
    # 셀은 strip 해서 비교하므로, 공백이 붙은 campaign 은 자기가 만든 열조차
    # 못 찾아 매 실행마다 새 열을 만들고 그때마다 전원에게 다시 보낸다.
    ws = _ws([_person("홍길동", "01011111111")])
    ledger.claim(ws, "discord", [{"to": "01011111111"}])

    won, _, blocked, _ = ledger.claim(ws, " discord ", [{"to": "01011111111"}])

    assert ws.rows[0] == HEADER + ["discord"]
    assert won == [] and len(blocked) == 1


def test_빈_칸인_사람만_보낸다():
    ws = _ws(
        [_person("홍길동", "01011111111"), _person("김철수", "01022222222")],
        header=HEADER + ["discord"],
    )
    ws.rows[1].append("2026-08-11 20:14")  # 홍길동은 이미 받음

    won, already, _, missing = ledger.claim(
        ws, "discord", [{"to": "01011111111"}, {"to": "01022222222"}]
    )

    assert [w["to"] for w in won] == ["01022222222"]
    assert [a["to"] for a in already] == ["01011111111"]
    assert missing == []


def test_사람이_손으로_적은_표시도_존중한다():
    # 장애 중 뿌리오 웹으로 보내고 시트에 아무 표시나 해둔 경우.
    ws = _ws([_person("홍길동", "01011111111")], header=HEADER + ["discord"])
    ws.rows[1].append("수기 발송")

    won, already, _, _ = ledger.claim(ws, "discord", [{"to": "01011111111"}])

    assert won == [] and len(already) == 1


def test_명단에_없으면_보내지_않고_알린다():
    # 조용히 빼면 안 간 줄 모르고, 그냥 보내면 기록할 곳이 없다.
    ws = _ws([_person("홍길동", "01011111111")])

    won, _, _, missing = ledger.claim(
        ws, "discord", [{"to": "01011111111"}, {"to": "01099999999"}]
    )

    assert [w["to"] for w in won] == ["01011111111"]
    assert [m["to"] for m in missing] == ["01099999999"]


def test_명단_표기가_달라도_대조된다():
    # 우리는 01011111111 로 쓰고 명단에는 사람이 010-1111-1111 로 적는다.
    ws = _ws([_person("홍길동", "010-1111-1111")])

    won, _, _, missing = ledger.claim(ws, "discord", [{"to": "01011111111"}])

    assert len(won) == 1 and missing == []


def test_보내기_전에_칸을_선점한다():
    # 선점하지 않으면 그 사이 다른 실행이 같은 사람을 빈 칸으로 보고 또 보낸다.
    ws = _ws([_person("홍길동", "01011111111")])

    ledger.claim(ws, "discord", [{"to": "01011111111"}])

    assert ws.rows[1][4].startswith(ledger.SENDING)


def test_선점을_풀면_다시_대상이_된다():
    ws = _ws([_person("홍길동", "01011111111")])
    won, _, _, _ = ledger.claim(ws, "discord", [{"to": "01011111111"}])

    ledger.mark(ws, "discord", [w["to"] for w in won], "")

    won2, already, _, _ = ledger.claim(ws, "discord", [{"to": "01011111111"}])
    assert len(won2) == 1 and already == []


def test_폼_재응답으로_같은_번호가_두_줄이면_두_번_보내지_않는다():
    # 폼 재응답은 맨 아래에 새 줄로 붙고 그 줄의 캠페인 열은 늘 비어 있다.
    # 마지막 줄만 보면 재응답자 전원이 캠페인마다 두 번씩 받는다.
    ws = _ws([_person("홍길동", "010-1111-1111")], header=HEADER + ["discord"])
    ws.rows[1].append("2026-08-01 10:00")
    ws.rows.append(_person("홍길동", "010-1111-1111"))  # 같은 사람 재응답

    won, already, _, _ = ledger.claim(ws, "discord", [{"to": "01011111111"}])

    assert won == [] and len(already) == 1


def test_중복_줄에는_선점도_전부_쓴다():
    # 한 줄만 선점하면 나머지 줄이 빈 채로 남아 다음 실행이 또 보낸다.
    ws = _ws(
        [_person("홍길동", "010-1111-1111"), _person("홍길동", "01011111111")],
    )

    ledger.claim(ws, "discord", [{"to": "01011111111"}])

    at = _column(ws, "discord")
    assert all(row[at].startswith(ledger.SENDING) for row in ws.rows[1:])


def test_한_명도_대조되지_않으면_열을_만들지_않는다():
    # 번호 열을 잘못 잡아 전원이 명단 밖으로 빠진 실행이 남의 운영 시트에
    # 빈 열만 남기면, 인자를 고쳐 다시 돌릴 때마다 열이 하나씩 늘어난다.
    ws = _ws([_person("홍길동", "01011111111")])

    won, _, _, missing = ledger.claim(ws, "discord", [{"to": "01099999999"}])

    assert won == [] and len(missing) == 1
    assert ws.rows[0] == HEADER


def test_번호_열_제목을_캠페인으로_주면_거절한다():
    # 그대로 두면 발송 기록이 번호를 덮어써 명단이 복구 불가능해진다.
    ws = _ws([_person("홍길동", "01011111111")])

    with pytest.raises(ledger.RosterLayoutError, match="번호 열"):
        ledger.claim(ws, "휴대폰", [{"to": "01011111111"}])


def test_발송중은_끝난_것과_갈라_돌려준다():
    # '발송중' 은 접수 여부를 모르는 상태다. 끝난 것과 섞으면 사람이
    # "이미 발송됨"으로 읽고 접수도 안 된 캠페인을 끝난 것으로 안다.
    ws = _ws(
        [_person("가", "01011111111"), _person("나", "01022222222")],
        header=HEADER + ["discord"],
    )
    ws.rows[1].append(f"{ledger.SENDING} 2026-08-11 20:14")
    ws.rows[2].append("2026-08-11 20:14")

    won, done, blocked, _ = ledger.claim(
        ws, "discord", [{"to": "01011111111"}, {"to": "01022222222"}]
    )

    assert won == []
    assert [b["to"] for b in blocked] == ["01011111111"]
    assert [d["to"] for d in done] == ["01022222222"]


def test_같은_번호를_두_번_넘기면_거절한다():
    ws = _ws([_person("홍길동", "01011111111")])

    with pytest.raises(ValueError, match="같은 번호"):
        ledger.claim(ws, "discord", [{"to": "01011111111"}, {"to": "01011111111"}])


def test_선점_뒤에_행이_지워져도_남의_칸에_쓰지_않는다():
    # 폼 응답 시트는 사람이 열어둔 채 응답을 지운다. 선점 때의 행 번호를
    # 그대로 쓰면 밀린 자리에 써서, 받은 사람은 '발송중'으로 남고 안 받은
    # 사람 칸에 발송 시각이 찍힌다 — 그 사람은 영영 못 받는다.
    ws = _ws(
        [
            _person("가", "01011111111"),
            _person("나", "01022222222"),
            _person("다", "01033333333"),
        ]
    )
    won, _, _, _ = ledger.claim(
        ws, "discord", [{"to": "01022222222"}, {"to": "01033333333"}]
    )
    del ws.rows[1]  # '가' 응답이 지워져 아래가 한 칸씩 올라온다

    ledger.mark(ws, "discord", [w["to"] for w in won], "2026-08-11 20:14")

    at = _column(ws, "discord")
    marked = {row[2]: row[at] for row in ws.rows[1:]}
    assert marked == {
        "01022222222": "2026-08-11 20:14",
        "01033333333": "2026-08-11 20:14",
    }


def test_선점_뒤에_열이_생겨도_번호_열을_덮지_않는다():
    # 폼에 문항이 추가되면 열이 삽입되어 캠페인 열이 오른쪽으로 밀린다.
    # 선점 때의 열 번호를 그대로 쓰면 그 자리에 있던 열을 덮는다.
    ws = _ws([_person("홍길동", "01011111111")])
    won, _, _, _ = ledger.claim(ws, "discord", [{"to": "01011111111"}])
    for row in ws.rows:  # 새 문항이 맨 앞에 끼어든 상황
        row.insert(0, "새 문항")

    ledger.mark(ws, "discord", [w["to"] for w in won], "2026-08-11 20:14")

    assert ws.rows[0] == ["새 문항"] + HEADER + ["discord"]
    assert ws.rows[1][3] == "01011111111"
    assert ws.rows[1][_column(ws, "discord")] == "2026-08-11 20:14"


def test_주소를_붙여넣어도_ID와_탭을_뽑는다():
    assert ledger.parse_spreadsheet_ref(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLdcOffdBENZ8vWJT9X1zku/"
        "edit?gid=1077887383#gid=1077887383"
    ) == ("1hW3Yg8x99gfiLdcOffdBENZ8vWJT9X1zku", 1077887383)


def test_ID를_그대로_줘도_받는다():
    assert ledger.parse_spreadsheet_ref("  1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd \n") == (
        "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd",
        None,
    )


def test_게시용_링크는_ID로_보지_않는다():
    with pytest.raises(ValueError):
        ledger.parse_spreadsheet_ref(
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vAbC/pubhtml"
        )


def test_시트로_읽히지_않으면_거절한다():
    with pytest.raises(ValueError):
        ledger.parse_spreadsheet_ref("그 시트요")


# 아래는 실제 운영 시트("기업연계 정보 교원 사업 참여 조사(응답)")의 헤더다.
# 구글 폼 응답 시트라 열 제목이 설문 문항 그대로이고, 우리가 가정한
# '휴대폰' 같은 짧은 이름이 하나도 없다. 이 헤더로 안 돌면 현장에서 못 쓴다.
FORM_HEADER = [
    "타임스탬프",
    "이메일 주소",
    "휴대전화 번호",
    "소속 학교명",
    "성함",
    "담당교과",
    "학교급",
    "시·도 교육청",
    "교직경력(N년)",
    "표시 과목이 *정보·컴퓨터*입니까?",
    "2024~2025년 디지털 활용 연수 이수 이력 (복수 선택 가능)",
    "실습 희망 트랙(희망 트랙에 따라 실습해보는 활동내용이 달라집니다)",
    "희망 기수 (15차시 기반연수)",
    "기반연수 숙박 신청(2인 1실 배정)",
    "코딩 경험 자가 진단",
    "AI 도구 활용 경험 (복수 선택 가능)",
    "본인이 *해결하고 싶은 수업 문제*를 한 줄로 적어주세요",
    "개인정보 수집·이용 동의",
    "운영진에 전달하고 싶은 의견·질문이 있으면 자유롭게 적어주세요",
    "성별",
    "비고",
    "연수 참여에 대한 공문 발송 여부",
    "발송여부",
    "문자발송",
]


def _form_row(phone: str, name: str, 문자발송: str = "") -> list:
    row = [""] * len(FORM_HEADER)
    row[2] = phone
    row[4] = name
    row[23] = 문자발송
    return row


def test_폼_응답_시트에서_휴대전화_번호_열을_찾는다():
    # '휴대전화 번호' 는 띄어쓰기 때문에 후보 '전화번호' 로는 안 잡힌다.
    # '휴대전화' 가 잡아야 하고, 그보다 뒤에 있는 '번호' 가 먼저 걸리면
    # 엉뚱한 열을 번호로 읽는다.
    header, at, by_phone = ledger._parse_roster(
        [FORM_HEADER, _form_row("010-6245-4553", "김태서")]
    )

    assert header[at] == "휴대전화 번호"
    assert by_phone == {"01062454553": [2]}


def test_폼_응답_시트에서_캠페인_열은_맨_뒤에_붙는다():
    # 폼이 응답을 추가할 때 건드리지 않는 자리여야 한다.
    ws = FakeWorksheet([FORM_HEADER, _form_row("010-6245-4553", "김태서")])

    ledger.claim(ws, "discord안내", [{"to": "01062454553"}])

    assert ws.rows[0][len(FORM_HEADER)] == "discord안내"


def test_캠페인_열이_Z를_넘어도_쓴다():
    # 실제 명단이 24열이라 캠페인 세 개면 AA 열에 닿는다. A1 표기를 한 글자로
    # 파싱하면 거기서 죽는다.
    ws = FakeWorksheet([FORM_HEADER, _form_row("010-6245-4553", "김태서")])
    for campaign in ("1차", "2차", "3차"):
        ledger.claim(ws, campaign, [{"to": "01062454553"}])
        ledger.mark(ws, campaign, ["01062454553"], "2026-08-11 20:14")

    assert ws.rows[0][26] == "3차"
    assert ws.rows[1][26] == "2026-08-11 20:14"


def test_사람이_쓰던_문자발송_열을_그대로_쓴다():
    # 이미 손으로 '7.8' 을 적어둔 열이다. 같은 이름을 campaign 으로 주면
    # 새 열을 만들지 않고 그 값을 "이미 보냄"으로 읽어야 한다.
    ws = FakeWorksheet(
        [
            FORM_HEADER,
            _form_row("010-6245-4553", "김태서", 문자발송="7.8"),
            _form_row("010-2273-1905", "안베티"),
        ]
    )

    won, already, _, missing = ledger.claim(
        ws, "문자발송", [{"to": "01062454553"}, {"to": "01022731905"}]
    )

    assert len(ws.rows[0]) == len(FORM_HEADER)
    assert [item["to"] for item in already] == ["01062454553"]
    assert [item["to"] for item in won] == ["01022731905"]
    assert missing == []
