"""명단 컬럼 기록 테스트 — 빈 칸인 사람만 보낸다는 규칙을 고정한다.

시트에는 UNIQUE 제약도 조건부 쓰기도 없다. "그 열이 비어 있으면 아직 안 보낸
사람"이 유일한 중복 차단 장치이고, 벤더를 부르기 전에 칸을 선점하는 것이
동시 실행을 막는 전부다.
"""

import pytest

from tests.fakes_sheets import FakeWorksheet

from service.sms import ledger

HEADER = ["연번", "성명", "휴대폰", "소속학교"]


def _ws(rows: list[list] | None = None, header: list[str] | None = None):
    return FakeWorksheet([header or HEADER] + (rows or []))


def _person(name: str, phone: str) -> list:
    return ["1", name, phone, "OO중학교"]


def test_번호_열을_이름으로_찾는다():
    ws = _ws([_person("홍길동", "010-1111-1111")])
    header, people = ledger.read_roster(ws)

    assert header == HEADER
    assert people == [{"phone": "01011111111", "_row": 2}]


def test_연락처든_휴대전화든_찾는다():
    ws = _ws([["1", "홍길동", "010-1111-1111"]], header=["연번", "성명", "연락처"])
    _, people = ledger.read_roster(ws)

    assert people[0]["phone"] == "01011111111"


def test_연번이_번호로_잡히지_않는다():
    # 이름 후보를 셀보다 바깥에서 돌지 않으면 '번호'가 연번에 걸려
    # 모든 수신자가 엉뚱한 값으로 대조된다.
    ws = _ws([["1", "홍길동", "010-1111-1111"]], header=["연번", "성명", "휴대폰"])
    _, people = ledger.read_roster(ws)

    assert people[0]["phone"] == "01011111111"


def test_번호_열이_없으면_거절한다():
    # 조용히 빈 명단으로 넘어가면 아무에게도 안 보내고 성공으로 끝난다.
    with pytest.raises(ledger.RosterLayoutError):
        ledger.read_roster(_ws(header=["연번", "성명", "소속"]))


def test_캠페인_열이_없으면_맨_뒤에_만든다():
    # 중간에 끼우면 사람이 만든 수식과 조건부 서식이 밀린다.
    ws = _ws([_person("홍길동", "01011111111")])
    header, _ = ledger.read_roster(ws)

    at = ledger.campaign_column(ws, header, "discord")

    assert at == len(HEADER)
    assert ws.rows[0][at] == "discord"


def test_같은_캠페인_열을_다시_만들지_않는다():
    ws = _ws([_person("홍길동", "01011111111")], header=HEADER + ["discord"])
    header, _ = ledger.read_roster(ws)

    assert ledger.campaign_column(ws, header, "discord") == 4
    assert len(ws.rows[0]) == 5


def test_빈_칸인_사람만_보낸다():
    ws = _ws(
        [_person("홍길동", "01011111111"), _person("김철수", "01022222222")],
        header=HEADER + ["discord"],
    )
    ws.rows[1].append("2026-08-11 20:14")  # 홍길동은 이미 받음
    header, _ = ledger.read_roster(ws)

    won, already, missing = ledger.claim(
        ws, header, "discord", [{"to": "01011111111"}, {"to": "01022222222"}]
    )

    assert [w["to"] for w in won] == ["01022222222"]
    assert [a["to"] for a in already] == ["01011111111"]
    assert missing == []


def test_사람이_손으로_적은_표시도_존중한다():
    # 장애 중 뿌리오 웹으로 보내고 시트에 아무 표시나 해둔 경우.
    ws = _ws([_person("홍길동", "01011111111")], header=HEADER + ["discord"])
    ws.rows[1].append("수기 발송")
    header, _ = ledger.read_roster(ws)

    won, already, _ = ledger.claim(ws, header, "discord", [{"to": "01011111111"}])

    assert won == [] and len(already) == 1


def test_명단에_없으면_보내지_않고_알린다():
    # 조용히 빼면 안 간 줄 모르고, 그냥 보내면 기록할 곳이 없다.
    ws = _ws([_person("홍길동", "01011111111")])
    header, _ = ledger.read_roster(ws)

    won, _, missing = ledger.claim(
        ws, header, "discord", [{"to": "01011111111"}, {"to": "01099999999"}]
    )

    assert [w["to"] for w in won] == ["01011111111"]
    assert [m["to"] for m in missing] == ["01099999999"]


def test_명단_표기가_달라도_대조된다():
    # 우리는 01011111111 로 쓰고 명단에는 사람이 010-1111-1111 로 적는다.
    ws = _ws([_person("홍길동", "010-1111-1111")])
    header, _ = ledger.read_roster(ws)

    won, _, missing = ledger.claim(ws, header, "discord", [{"to": "01011111111"}])

    assert len(won) == 1 and missing == []


def test_보내기_전에_칸을_선점한다():
    # 선점하지 않으면 그 사이 다른 실행이 같은 사람을 빈 칸으로 보고 또 보낸다.
    ws = _ws([_person("홍길동", "01011111111")])
    header, _ = ledger.read_roster(ws)

    ledger.claim(ws, header, "discord", [{"to": "01011111111"}])

    assert ws.rows[1][4].startswith(ledger.SENDING)


def test_선점을_풀면_다시_대상이_된다():
    ws = _ws([_person("홍길동", "01011111111")])
    header, _ = ledger.read_roster(ws)
    won, _, _ = ledger.claim(ws, header, "discord", [{"to": "01011111111"}])

    ledger.mark(ws, header, "discord", [w["_row"] for w in won], "")

    won2, already, _ = ledger.claim(ws, header, "discord", [{"to": "01011111111"}])
    assert len(won2) == 1 and already == []


def test_같은_번호를_두_번_넘기면_거절한다():
    ws = _ws([_person("홍길동", "01011111111")])
    header, _ = ledger.read_roster(ws)

    with pytest.raises(ValueError, match="같은 번호"):
        ledger.claim(
            ws, header, "discord", [{"to": "01011111111"}, {"to": "01011111111"}]
        )


def test_주소를_붙여넣어도_ID를_뽑는다():
    assert (
        ledger.parse_spreadsheet_id(
            "https://docs.google.com/spreadsheets/d/1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd/edit#gid=0"
        )
        == "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_ID를_그대로_줘도_받는다():
    assert (
        ledger.parse_spreadsheet_id("  1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd \n")
        == "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_게시용_링크는_ID로_보지_않는다():
    with pytest.raises(ValueError):
        ledger.parse_spreadsheet_id(
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vAbC/pubhtml"
        )


def test_시트로_읽히지_않으면_거절한다():
    with pytest.raises(ValueError):
        ledger.parse_spreadsheet_id("그 시트요")


def test_주소의_탭_ID를_뽑는다():
    assert (
        ledger.parse_gid(
            "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLdcOffdBENZ8vWJT9X1zku/"
            "edit?gid=1077887383#gid=1077887383"
        )
        == 1077887383
    )


def test_탭_ID가_없으면_None():
    assert ledger.parse_gid("1hW3Yg8x99gfiLdcOffdBENZ8vWJT9X1zku") is None


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
    ws = FakeWorksheet([FORM_HEADER, _form_row("010-6245-4553", "김태서")])
    header, people = ledger.read_roster(ws)

    assert header[2] == "휴대전화 번호"
    assert people == [{"phone": "01062454553", "_row": 2}]


def test_폼_응답_시트에서_캠페인_열은_맨_뒤에_붙는다():
    # 폼이 응답을 추가할 때 건드리지 않는 자리여야 한다.
    ws = FakeWorksheet([FORM_HEADER, _form_row("010-6245-4553", "김태서")])
    header, _ = ledger.read_roster(ws)

    at = ledger.campaign_column(ws, header, "discord안내")

    assert at == len(FORM_HEADER)
    assert ws.rows[0][at] == "discord안내"


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
    header, _ = ledger.read_roster(ws)

    won, already, missing = ledger.claim(
        ws, header, "문자발송", [{"to": "01062454553"}, {"to": "01022731905"}]
    )

    assert len(ws.rows[0]) == len(FORM_HEADER)
    assert [item["to"] for item in already] == ["01062454553"]
    assert [item["to"] for item in won] == ["01022731905"]
    assert missing == []
