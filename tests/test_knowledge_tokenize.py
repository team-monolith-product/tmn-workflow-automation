"""IDF 토큰화 테스트

픽스처는 실제 사내 채널에 올라온 문장에서 가져왔습니다.
"""

from service.knowledge.tokenize import strip_josa, tokenize


def test_식별자는_통째로_남는다():
    tokens = tokenize("karafka-rdkafka GLIBC 오류는 CVE-2026-40295 와 무관합니다")
    assert "karafka-rdkafka" in tokens
    assert "cve-2026-40295" in tokens


def test_리소스_id도_한_토큰이다():
    assert "sg-09ddf59899f05a71a" in tokenize("보안그룹 sg-09ddf59899f05a71a 확인")


def test_ascii는_소문자로_정규화된다():
    assert "glibc" in tokenize("GLIBC 버전")
    assert "GLIBC" not in tokenize("GLIBC 버전")


def test_조사가_달라도_같은_토큰이_된다():
    a = tokenize("지식체계를 구축한다")
    b = tokenize("지식체계는 어렵다")
    c = tokenize("지식체계가 필요하다")
    assert "지식체계" in a and "지식체계" in b and "지식체계" in c


def test_긴_조사도_떨어진다():
    assert "스레드" in tokenize("스레드에서 답을 찾는다")
    assert "채널" in tokenize("채널부터 정리하자")
    assert "요약" in tokenize("요약으로부터 복원한다")


def test_두_글자를_못_남기면_조사를_떼지_않는다():
    # "의도"에서 "도"를 떼면 "의"만 남는다. 이런 파괴를 막는다.
    assert strip_josa("의도") == "의도"
    assert strip_josa("정도") == "정도"
    assert strip_josa("회의") == "회의"
    assert strip_josa("평가") == "평가"


def test_조사가_아닌_끝음절은_건드리지_않는다():
    assert strip_josa("도구") == "도구"
    assert strip_josa("검사") == "검사"


def test_복수와_조사가_겹쳐도_어간까지_간다():
    assert strip_josa("선생님들께") == "선생님"
    assert strip_josa("학생들이") == "학생"
    assert strip_josa("코드로는") == "코드"


def test_반복_횟수를_막아_과도한_삭감을_피한다():
    # 한도가 없으면 "바나나"가 "바"까지 깎인다
    assert len(strip_josa("바나나")) >= 2
    assert strip_josa("하나") == "하나"


def test_한_글자_토큰은_버린다():
    assert all(len(t) >= 2 for t in tokenize("이 건 좀 더 봐 야 함 a b c"))


def test_같은_토큰이_여러_번_나와도_하나로_친다():
    # 문서빈도를 세는 용도라 문서 안 중복은 의미가 없다
    assert len([t for t in tokenize("배포 배포 배포") if t == "배포"]) == 1


def test_실제_운영_메시지():
    text = (
        "8/3 현장에서 선생님들께 전체 일정을 안내하려면 오늘~내일 중 컨소 합의가 "
        "필요합니다. 예선을 9/7~10/11로 잡은 이유는 추석이 통째로 들어가기 때문입니다."
    )
    tokens = tokenize(text)
    # 조사와 복수를 떼야 연수 채널에서 흔한 "선생님"이 흔하게 집계된다.
    # 안 떼면 변이마다 흩어져 IDF가 부풀고 게이트가 무력해진다.
    assert "선생님" in tokens
    assert "선생님들께" not in tokens
    assert "현장" in tokens
    assert "일정" in tokens
    assert "추석" in tokens


def test_실제_기술_메시지():
    text = (
        "PXT 측 빈 projects 처리는 graceful합니다. "
        "webapp/src/app.tsx 의 hasError 상태가 MAKECODE_VF_EDITOR 설정과 불일치하면 "
        "에러 바운더리 화면이 뜹니다."
    )
    tokens = tokenize(text)
    # 임베딩이 뭉개는 고유 문자열이 IDF에서 변별력을 준다
    assert "makecode_vf_editor" in tokens
    assert "haserror" in tokens
    assert "webapp" in tokens
