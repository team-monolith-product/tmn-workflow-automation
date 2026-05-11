from app.route_deal import parse_form_submission

SAMPLE_MESSAGE_MULTI_PLAN = (
    "코들 문의 신청 새 제출 *제출 시각*\n"
    "2026-05-11 15:08 *학교명*\n"
    "군산대성중학교\n\n"
    "*성함*\n"
    "양성용\n\n"
    "*휴대전화번호*\n"
    "<tel:010-2012-3170|010-2012-3170>\n\n"
    "*코들을 알게 된 경로*\n"
    "기타\n\n"
    "*개인정보 수집·이용 동의*\n"
    "동의\n\n"
    "*Pro 플랜 (학생 수)*\n"
    "30\n\n"
    "*Pro 플랜 (사용 학기 수)*\n"
    "1,2\n\n"
    "*Pro 플랜 (가격)*\n"
    "7128000\n\n"
    "*씨마스, 비상 플랜 (학생 수)*\n"
    "30\n\n"
    "*씨마스, 비상 플랜 (사용 학기 수)*\n"
    "1,2 어드민 링크: <https://admin.codle.io/form_submissions/8/show>"
)

SAMPLE_MESSAGE_SINGLE_PLAN = (
    "코들 문의 신청 새 제출 *제출 시각*\n"
    "2026-05-11 13:31 *학교명*\n"
    "저현고등학교\n\n"
    "*성함*\n"
    "고객팀(테스트2)\n\n"
    "*휴대전화번호*\n"
    "<tel:010-2599-9521|010-2599-9521>\n\n"
    "*코들을 알게 된 경로*\n"
    "온·오프라인 연수\n\n"
    "*개인정보 수집·이용 동의*\n"
    "동의\n\n"
    "*AI 플랜 (학생 수)*\n"
    "1\n\n"
    "*AI 플랜 (사용 학기 수)*\n"
    "1\n\n"
    "*AI 플랜 (가격)*\n"
    "40000 어드민 링크: <https://admin.codle.io/form_submissions/7/show>"
)

SAMPLE_MESSAGE_TWO_PLANS_WITH_PRICE = (
    "코들 문의 신청 새 제출 *제출 시각*\n"
    "2026-05-11 13:30 *학교명*\n"
    "저현고등학교\n\n"
    "*성함*\n"
    "고객팀(테스트)\n\n"
    "*휴대전화번호*\n"
    "<tel:010-2599-9521|010-2599-9521>\n\n"
    "*코들을 알게 된 경로*\n"
    "기타\n\n"
    "*개인정보 수집·이용 동의*\n"
    "동의\n\n"
    "*Pro 플랜 (학생 수)*\n"
    "12\n\n"
    "*Pro 플랜 (사용 학기 수)*\n"
    "1\n\n"
    "*Pro 플랜 (가격)*\n"
    "264000\n\n"
    "*AI 플랜 (학생 수)*\n"
    "30\n\n"
    "*AI 플랜 (사용 학기 수)*\n"
    "2\n\n"
    "*AI 플랜 (가격)*\n"
    "2160000 어드민 링크: <https://admin.codle.io/form_submissions/6/show>"
)


def test_parse_multi_plan():
    result = parse_form_submission(SAMPLE_MESSAGE_MULTI_PLAN)
    assert result is not None
    assert result["school"] == "군산대성중학교"
    assert result["name"] == "양성용"
    assert result["phone"] == "010-2012-3170"
    assert result["source"] == "기타"
    assert len(result["plans"]) == 2
    assert result["plans"][0]["name"] == "Pro 플랜"
    assert result["plans"][0]["students"] == 30
    assert result["plans"][0]["semesters"] == "1,2"
    assert result["plans"][0]["price"] == 7128000
    assert result["plans"][1]["name"] == "씨마스, 비상 플랜"
    assert result["plans"][1]["students"] == 30
    assert result["total_students"] == 60
    assert result["total_price"] == 7128000
    assert result["admin_link"] == "https://admin.codle.io/form_submissions/8/show"


def test_parse_single_plan():
    result = parse_form_submission(SAMPLE_MESSAGE_SINGLE_PLAN)
    assert result is not None
    assert result["school"] == "저현고등학교"
    assert result["name"] == "고객팀(테스트2)"
    assert result["phone"] == "010-2599-9521"
    assert len(result["plans"]) == 1
    assert result["plans"][0]["name"] == "AI 플랜"
    assert result["plans"][0]["students"] == 1
    assert result["plans"][0]["price"] == 40000
    assert result["total_students"] == 1
    assert result["total_price"] == 40000


def test_parse_two_plans_with_price():
    result = parse_form_submission(SAMPLE_MESSAGE_TWO_PLANS_WITH_PRICE)
    assert result is not None
    assert result["school"] == "저현고등학교"
    assert len(result["plans"]) == 2
    assert result["plans"][0]["name"] == "Pro 플랜"
    assert result["plans"][0]["students"] == 12
    assert result["plans"][0]["price"] == 264000
    assert result["plans"][1]["name"] == "AI 플랜"
    assert result["plans"][1]["students"] == 30
    assert result["plans"][1]["price"] == 2160000
    assert result["total_students"] == 42
    assert result["total_price"] == 2424000


def test_parse_non_form_message():
    result = parse_form_submission("그냥 메시지입니다")
    assert result is None


def test_parse_missing_school():
    text = "코들 문의 신청 새 제출 *성함*\n홍길동"
    result = parse_form_submission(text)
    assert result is None
