"""발송 CLI 의 입력 관문 테스트.

spreadsheet_id 는 엉뚱한 시트를 여는 걸 막는 유일한 장치이고, read_csv 는
"문제를 전부 모아 돌려준다"는 check 의 계약이 성립하려면 어떤 CSV 를 받아도
죽지 않아야 한다. 둘 다 비자명한데 테스트가 없어 수정이 그대로 되돌아갈 수
있었다.
"""

import pytest

from scripts import send_sms


def test_주소를_붙여넣어도_ID를_뽑는다():
    assert (
        send_sms.spreadsheet_id(
            "https://docs.google.com/spreadsheets/d/1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd/edit#gid=0"
        )
        == "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_ID를_그대로_줘도_받는다():
    assert (
        send_sms.spreadsheet_id("  1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd \n")
        == "1ceFWQKdOQXgbII6lZIV2ruuyWR_gBZyd"
    )


def test_시트로_읽히지_않으면_거절한다():
    # 통과시키면 엉뚱한 ID 로 시트를 열려다 발송 직전에 죽는다.
    with pytest.raises(ValueError):
        send_sms.spreadsheet_id("그 시트요")


def test_번호_칸이_비어도_검사_경로로_들어온다(tmp_path):
    # 여기서 죽으면 check 가 문제를 모아 돌려주지 못하고 트레이스백이 뜬다.
    path = tmp_path / "roster.csv"
    path.write_text("to,name,var1\n,홍길동,1기\n010-2222-2222,김철수,2기\n")

    rows = send_sms.read_csv(path)

    assert rows[0]["to"] == ""
    problems = send_sms.sms_send.check(rows, template_name="discord")
    assert any("형식 오류" in problem for problem in problems)


def test_필드가_모자란_행도_빈_문자열로_읽는다(tmp_path):
    # DictReader 기본 restval 은 None 이라 정규식이 TypeError 로 죽는다.
    path = tmp_path / "roster.csv"
    path.write_text("name,to\n홍길동\n")

    assert send_sms.read_csv(path)[0]["to"] == ""


def test_헤더가_다르면_기대_헤더를_알려준다(tmp_path):
    # 참가자 시트를 그대로 내보내면 헤더가 번호,이름 이다.
    path = tmp_path / "roster.csv"
    path.write_text("번호,이름\n010-1111-1111,홍길동\n")

    with pytest.raises(ValueError, match="to"):
        send_sms.read_csv(path)
