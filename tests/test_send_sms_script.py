"""발송 CLI 의 입력 관문 테스트.

spreadsheet_id 는 엉뚱한 시트를 여는 걸 막는 유일한 장치이고, read_csv 는
"문제를 전부 모아 돌려준다"는 check 의 계약이 성립하려면 어떤 CSV 를 받아도
죽지 않아야 한다. 둘 다 비자명한데 테스트가 없어 수정이 그대로 되돌아갈 수
있었다.
"""

import pytest

from scripts import send_sms


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


def test_치환값_열_이름이_틀리면_거절한다(tmp_path):
    # to 만 보면 나머지가 틀려도 통과한다. 그러면 changeWord 가 통째로 안 실려
    # 수신자는 [*1*] 자리가 빈 문자를 받는데, 발송이 끝난 뒤에야 알게 되고
    # 그 campaign 은 이미 잡혀 정정 발송도 막힌다.
    path = tmp_path / "roster.csv"
    path.write_text("to,name,기수,링크\n010-1111-1111,홍길동,1기,https://x\n")

    with pytest.raises(ValueError, match="var1"):
        send_sms.read_csv(path)


def test_한글_엑셀이_저장한_CP949도_읽는다(tmp_path):
    # 이 CLI 를 쓸 사람이 가장 흔히 만드는 파일이다. utf-8 로 고정하면
    # 명단을 만들자마자 트레이스백을 본다.
    path = tmp_path / "roster.csv"
    path.write_bytes("to,name\n010-1111-1111,홍길동\n".encode("cp949"))

    assert send_sms.read_csv(path)[0]["name"] == "홍길동"


def test_열이_밀린_줄은_거절한다(tmp_path):
    # 따옴표 없는 쉼표로 열이 밀리면 초과 필드가 조용히 버려지고 헤더 검사는
    # 통과한다. 그대로 두면 치환값이 한 칸씩 밀린 채 발송된다.
    path = tmp_path / "roster.csv"
    path.write_text("to,name,var1\n010-1111-1111,홍길동, 팀장,1기\n")

    with pytest.raises(ValueError, match="열 수가 헤더보다 많은"):
        send_sms.read_csv(path)
