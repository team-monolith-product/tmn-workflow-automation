"""발송 계층 테스트."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from service.sms import send as sms_send
from service.sms import templates
from service.sms import transport

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기"},
]
CONTENT = "[*이름*]선생님, [*1*] 안내드립니다"


def _run(monkeypatch, response=None, boom=None, **extra):
    captured = {}

    def fake_send(payload):
        captured.update(payload)
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    # 발신번호도 벤더 계층에서 온다. 목킹하지 않으면 환경변수를 찾는다.
    monkeypatch.setattr(transport, "sender", lambda: "01077647538")
    result = sms_send.send(rows=ROWS, content=f"\n{CONTENT}\n", **extra)
    return result, captured


def _accept(monkeypatch, captured=None):
    """벤더가 접수한 것으로 두고, 나간 payload 를 잡는다."""

    def fake_send(payload):
        if captured is not None:
            captured.update(payload)
        return {"code": "1000", "messageKey": "K"}

    monkeypatch.setattr(transport, "send", fake_send)
    monkeypatch.setattr(transport, "sender", lambda: "01077647538")


def test_한_번의_요청으로_전원에게_보낸다(monkeypatch):
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result.sent == 2
    assert result.message_key == "K1"
    assert payload["targetCount"] == 2
    assert [t["to"] for t in payload["targets"]] == ["01011111111", "01022222222"]


def test_벤더_필수_필드가_빠지지_않는다(monkeypatch):
    # duplicateFlag·refKey 가 빠져 400 code 2000 으로 전건이 거절된 적이 있습니다(8/21).
    # 카드는 정상으로 보여 발송된 줄 알았습니다 — 실패가 눈에 띄지 않는 종류입니다.
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    for field in (
        "messageType",
        "content",
        "duplicateFlag",
        "refKey",
        "targetCount",
        "targets",
    ):
        assert field in payload, f"벤더 필수 필드가 빠졌다: {field}"


def test_치환값이_벤더로_넘어간다(monkeypatch):
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert payload["targets"][0]["changeWord"]["var1"] == "1기"
    assert payload["targets"][0]["name"] == "가"


def test_벤더로_나가는_것은_치환_전_원문이다(monkeypatch):
    # 이력이 남기는 result.content 가 벤더로 나간 값과 같아야 한다. 갈리면
    # 앞뒤 공백 하나로 같은 문안이 DB 에서 둘로 쪼개진다.
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert payload["content"] == result.content == CONTENT
    # 이력이 남기는 targets 도 벤더로 나간 값이어야 한다. plan.rows 로 새면
    # change_word 가 전 행 NULL 이 되고, 기수·치환값은 재구성할 수 없다.
    assert payload["targets"] == result.targets


def test_같은_번호는_접어서_한_번만_보낸다(monkeypatch):
    _accept(monkeypatch)

    result = sms_send.send(
        rows=[{"to": "010-1111-1111"}, {"to": "01011111111"}], content="안녕"
    )

    assert result.sent == 1


def test_벤더가_거절하면_터뜨린다(monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, boom=transport.PpurioError(400, "bad request"))


def test_접수코드가_1000이_아니면_실패로_본다(monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, {"code": "2000", "description": "invalid"})


def test_LMS면_제목을_붙인다(monkeypatch):
    # 뿌리오는 LMS 에 subject 가 없으면 거절한다. payload 를 안 보면 이 두
    # 줄을 지워도 전부 초록이고, 90byte 넘는 문안이 실계정에서만 터진다.
    long_body = "가" * 100  # EUC-KR 200byte — SMS 90byte 한도를 넘는다
    captured = {}
    _accept(monkeypatch, captured)

    result = sms_send.send(rows=ROWS, content=long_body)

    assert result.message_type == "LMS"
    assert captured["subject"] == "안내"


def test_제목을_주면_그것을_싣는다(monkeypatch):
    captured = {}
    _accept(monkeypatch, captured)

    sms_send.send(rows=ROWS, content="가" * 100, subject="공지")

    assert captured["subject"] == "공지"


def test_SMS면_제목을_붙이지_않는다(monkeypatch):
    captured = {}
    _accept(monkeypatch, captured)

    sms_send.send(rows=ROWS, content=CONTENT)

    assert "subject" not in captured


def test_치환값이_태그보다_짧아도_원문_길이로_판정한다(monkeypatch):
    # 벤더로 나가는 것은 원문이고 벤더도 원문 길이로 타입을 본다. 치환 후만
    # 재면 SMS 로 판정해 놓고 90byte 넘는 원문을 보내게 된다.
    captured = {}
    _accept(monkeypatch, captured)
    # 원문 93byte, 치환 후 84byte 가 되도록 맞춘다.
    content = "[*이름*][*1*]" + "가" * 40
    rows = [{"to": "010-1111-1111", "name": "가", "var1": "나"}]

    sms_send.send(rows=rows, content=content)

    assert captured["messageType"] == "LMS"


def test_타입은_치환_후_최댓값으로_정한다():
    # 짧은 사람 기준으로 SMS 를 고르면 긴 사람만 발송에 실패한다.
    short = {"to": "01012345678", "name": "김"}
    long = {"to": "01012345679", "name": "김" * 60}

    assert sms_send.preview([short], "[*이름*]선생님").message_type == "SMS"
    assert sms_send.preview([short, long], "[*이름*]선생님").message_type == "LMS"


def test_LMS_한도를_넘으면_거절한다():
    huge = {"to": "01012345678", "name": "가" * 1100}

    assert "LMS 한도" in sms_send.preview([huge], "[*이름*]선생님").problems[0]


def test_수신자가_없으면_거른다():
    assert sms_send.preview([], CONTENT).problems == ["수신자가 없습니다."]


def test_문안이_비면_거른다():
    assert sms_send.preview(ROWS, "").problems == ["문안이 비어 있습니다."]


def test_공백뿐인_문안도_비어_있는_것으로_본다():
    # 검사를 정규화보다 먼저 하면 "\n\n" 이 통과해 빈 문자가 발송된다.
    assert sms_send.preview(ROWS, "\n\n").problems == ["문안이 비어 있습니다."]
    assert sms_send.preview(ROWS, "   ").problems == ["문안이 비어 있습니다."]


def test_번호가_없거나_수여도_형식_오류로_모은다():
    # 모델이 만든 목록이라 to 가 빠지거나 수로 올 수 있다. JSON 수는 선행 0 을
    # 못 써서 1011111111 로 오는데, 자릿수만 보면 통과해 그대로 벤더로 나간다.
    plan = sms_send.preview([{"name": "홍길동"}, {"to": 1011111111}], CONTENT)

    assert len(plan.problems) == 2


def test_문제를_한_번에_모아_돌려준다():
    plan = sms_send.preview([{"to": "010-123"}, {"to": "010-456"}], CONTENT)

    assert sum("형식 오류" in p for p in plan.problems) == 2


def test_보낼_수_있으면_problems가_비어_있다():
    assert sms_send.preview(ROWS, CONTENT).problems == []


def test_미리보기는_치환한_본문을_보여준다():
    plan = sms_send.preview(ROWS, CONTENT)

    assert plan.sample == "가선생님, 1기 안내드립니다"
    assert len(plan.rows) == 2
    assert plan.message_type == "SMS"


def test_보낼_수_없으면_send가_터진다(monkeypatch):
    monkeypatch.setattr(transport, "send", lambda payload: pytest.fail("막았어야 했다"))

    with pytest.raises(ValueError, match="형식 오류"):
        sms_send.send(rows=[{"to": "010-123"}], content=CONTENT)


def _later(minutes: int) -> str:
    """지금부터 N 분 뒤(한국 시간). 고정 문자열을 쓰면 날이 지나며 테스트가 썩는다."""
    when = datetime.now(tz=sms_send.KST) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def test_예약_시각이_벤더로_넘어간다(monkeypatch):
    _, payload = _run(
        monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=_later(60)
    )

    assert "sendTime" in payload
    # 벤더 형식이 아니면 조용히 즉시 발송되거나 거절된다.
    datetime.strptime(payload["sendTime"], sms_send.SEND_TIME_FORMAT)


def test_예약이_없으면_sendTime_을_싣지_않는다(monkeypatch):
    # 빈 값을 실으면 벤더가 형식 오류로 전건을 거절한다.
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert "sendTime" not in payload


def test_지난_시각은_보내기_전에_걸린다():
    plan = sms_send.preview(ROWS, CONTENT, send_at=_later(-10))

    assert plan.problems
    assert plan.send_at is None


def test_너무_촉박한_예약은_걸린다():
    # 벤더가 3분 미만을 거절한다. 여기서 안 걸면 승인 순간에야 실패한다.
    plan = sms_send.preview(ROWS, CONTENT, send_at=_later(1))

    assert plan.problems


def test_읽을_수_없는_시각은_걸린다():
    plan = sms_send.preview(ROWS, CONTENT, send_at="내일 아침")

    assert plan.problems


def test_시간대가_없으면_한국_시간으로_읽는다():
    # 서버가 UTC 로 돌면 naive 시각이 9시간 밀려, "오늘 오후" 예약이 내일 새벽이 된다.
    when = datetime.now(tz=sms_send.KST) + timedelta(hours=5)
    naive = when.strftime("%Y-%m-%d %H:%M:%S")

    parsed, problem = sms_send.parse_send_at(naive)

    assert problem is None
    assert parsed == when.replace(microsecond=0)


def test_예약해도_수신자와_문안은_그대로다(monkeypatch):
    _, payload = _run(
        monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=_later(60)
    )

    assert payload["targetCount"] == 2
    assert payload["content"] == CONTENT


def test_돌려주는_값이_벤더로_나간_값이다(monkeypatch):
    # 이력에서 발송 건수를 count(distinct ref_key) 로 세므로 벤더로 나간 키와
    # 같아야 한다. 발신번호도 호출부가 정하면 실제로 나간 번호와 갈린다.
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert result.ref_key == payload["refKey"]
    assert result.sender == "01077647538"


def test_merge_가_만든_빈_칸과_실수가_그대로_나가지_않는다():
    # 정수 열을 how="left" 로 merge 하면 짝 없는 칸이 NaN 이 되면서 열
    # 전체가 float 로 올라간다. 짝이 있는 칸은 3.0 이 된다. 손으로 NaN 을
    # 넣으면 뒤쪽 절반을 못 잡는다.
    명단 = pd.DataFrame(
        [
            {"to": "010-1111-2222", "name": "김철수"},
            {"to": "010-3333-4444", "name": "이영희"},
        ]
    )
    기수 = pd.DataFrame([{"to": "010-1111-2222", "var1": 3}])
    rows = 명단.merge(기수, on="to", how="left").to_dict("records")

    plan = sms_send.preview(rows, "[*이름*] 선생님, [*1*]기 안내입니다.")

    assert plan.problems == []
    assert plan.targets[0]["changeWord"]["var1"] == "3"
    assert plan.targets[1]["changeWord"]["var1"] == ""
    assert (
        templates.render(plan.template, plan.rows[0])
        == "김철수 선생님, 3기 안내입니다."
    )
    assert (
        templates.render(plan.template, plan.rows[1]) == "이영희 선생님, 기 안내입니다."
    )


def test_판다스_결측과_큰_수를_문자로_만들지_않는다():
    # iterrows 로 행을 만들면 nullable 열의 결측이 pd.NA 로 남는다. NA 는
    # 비교 결과마저 NA 라 참·거짓을 물으면 터진다. 2**53 을 넘는 float 은
    # 이미 정수를 정확히 못 담으므로 정수로 바꾸면 그럴듯한 오답이 된다.
    plan = sms_send.preview(
        [
            {"to": "010-1111-2222", "name": "가", "var1": pd.NA},
            {"to": "010-3333-4444", "name": "나", "var1": 1.2345678901234567e19},
        ],
        "[*이름*] [*1*]",
    )

    assert plan.problems == []
    assert plan.targets[0]["changeWord"]["var1"] == ""
    assert "e+19" in plan.targets[1]["changeWord"]["var1"]


def test_넘파이_정수가_빈_칸이_되지_않는다():
    # to_dict("records") 만 파이썬 정수로 박싱해 준다. df.loc·iloc·max·sum·
    # 산술은 전부 np.int64 라, 그 경로만 보면 나머지를 놓친다. 이 도구의
    # 용도가 집계인데 집계 결과가 치환값으로 들어오는 자리다.
    df = pd.DataFrame(
        [{"to": "010-1111-2222", "기수": 3}, {"to": "010-3333-4444", "기수": 5}]
    )

    plan = sms_send.preview(
        [{"to": "010-1111-2222", "name": "가", "var1": df["기수"].max()}],
        "[*이름*] 남은 자리 [*1*]석",
    )

    assert plan.targets[0]["changeWord"]["var1"] == "5"
    assert templates.render(plan.template, plan.rows[0]) == "가 남은 자리 5석"
