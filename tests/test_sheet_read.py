"""구글 시트 읽기 테스트 — 표가 밀리지 않고, 잘렸으면 잘렸다고 말해야 한다."""

import pytest

from service.sheets import read

VALUES = [
    ["타임스탬프", "휴대전화 번호", "소속 학교명", "성함", "출석(1일차 기준)"],
    ["2026. 8. 1", "010-1111-1111", "가중학교", "가", "출석"],
    ["2026. 8. 2", "010-2222-2222", "나고등학교", "나", "결석"],
]


def test_링크에서_시트와_탭을_뽑는다():
    sheet = read.parse_target(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit#gid=1234"
    )

    assert sheet.spreadsheet_id == "1hW3Yg8x99gfiLd"
    assert sheet.worksheet_id == 1234


def test_gid가_없으면_첫_탭이다():
    # None 과 0 을 뭉개면 안 된다. gid=0 은 "첫 탭" 이 아니라 "id 가 0 인 탭" 이고,
    # 시트를 지웠다 만들면 첫 탭의 gid 가 0 이 아니다.
    sheet = read.parse_target(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit"
    )

    assert sheet.worksheet_id is None


def test_쿼리_문자열_gid도_읽는다():
    # 슬랙이 링크를 다시 쓰면서 #gid 가 ?gid 로 바뀌어 온다.
    sheet = read.parse_target(
        "https://docs.google.com/spreadsheets/d/1hW3Yg8x99gfiLd/edit?gid=77#gid=77"
    )

    assert sheet.worksheet_id == 77


def test_ID만_줘도_된다():
    sheet = read.parse_target("1hW3Yg8x99gfiLdcOffdBENZ8vWJT9X1zkukUPHri1x0")

    assert sheet.spreadsheet_id == "1hW3Yg8x99gfiLdcOffdBENZ8vWJT9X1zkukUPHri1x0"
    assert sheet.worksheet_id is None


def test_시트_링크가_아니면_거절한다():
    with pytest.raises(ValueError):
        read.parse_target("https://team-mono.com/hello")


def test_열을_골라_읽는다():
    header, rows = read.pick(VALUES, ["성함", "휴대전화 번호"])

    assert header == ["성함", "휴대전화 번호"]
    # 고른 순서대로 나와야 짝이 맞는다.
    assert rows[0] == ["가", "010-1111-1111"]


def test_열_이름을_포함으로_찾는다():
    # 머리행은 "휴대전화 번호" 인데 사람은 "전화" 라고 부른다.
    header, _ = read.pick(VALUES, ["전화"])

    assert header == ["휴대전화 번호"]


def test_없는_열은_시트의_열_이름을_알려준다():
    # 그냥 실패하면 에이전트가 같은 이름으로 계속 다시 부른다.
    with pytest.raises(ValueError, match="휴대전화 번호"):
        read.pick(VALUES, ["연락처없음"])


def test_열을_비우면_전부_읽는다():
    header, rows = read.pick(VALUES, [])

    assert len(header) == 5
    assert len(rows) == 2


def test_빈_행은_버린다():
    # 시트 아래쪽은 빈 행이 수백 줄 이어진다. 세면 "297명" 이 된다.
    header, rows = read.pick(VALUES + [["", "", "", "", ""], []], ["성함"])

    assert len(rows) == 2


def test_셀_안의_탭과_줄나눔이_표를_밀지_않는다():
    # 탭은 열을 가르고 줄 나눔은 행을 가른다. 밀린 표는 엉뚱한 사람 번호로
    # 문자를 보내게 하고, 앞줄만 봐서는 보이지 않는다.
    values = [
        ["성함", "메모"],
        ["가", "첫 줄\n둘째 줄\t끝"],
    ]

    header, rows = read.pick(values, [])

    assert len(rows) == 1
    assert rows[0][1] == "첫 줄 둘째 줄 끝"


def test_짧은_행도_열을_채운다():
    # 시트 마지막 열이 비면 그 행은 짧게 온다. 인덱스로 집으면 터진다.
    values = [["성함", "학교", "비고"], ["가"]]

    _, rows = read.pick(values, [])

    assert rows[0] == ["가", "", ""]


def test_같은_머리행이_두_벌이면_값이_든_열을_고른다():
    # 폼에서 문항을 지웠다 다시 만들면 응답 시트에 옛 열이 그대로 남고, 새 응답은
    # 뒤에 붙은 열에 쌓인다. 앞엣것을 집으면 "명단 0명" 이 되어 아무에게도
    # 문자가 나가지 않는다 — 실패가 조용해서 더 위험하다(8/21 실측).
    values = [
        ["타임스탬프", "성함", "연락처", "성함", "연락처"],
        ["8/20 18:07", "", "", "이진선", "01086008593"],
        ["8/20 18:09", "", "", "김지수", "01028642153"],
    ]

    header, rows = read.pick(values, ["성함", "연락처"])

    assert header == ["성함", "연락처"]
    assert [row[0] for row in rows] == ["이진선", "김지수"]


def test_양쪽_다_비면_앞_열을_고른다():
    # 셀 수가 같으면 원래 순서를 흔들지 않는다.
    values = [["성함", "성함"], ["", ""]]

    header, rows = read.pick(values, ["성함"])

    assert header == ["성함"]
    assert rows == []
