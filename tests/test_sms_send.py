"""발송 게이트 테스트 — 자리를 먼저 잡고 보내는 순서가 지켜지는지 본다."""

import datetime
import urllib.error

import pytest

from tests.fakes_sheets import FakeWorksheet

from service.sms import KST, ledger
from service.sms import send as sms_send
from service.sms import transport

ROWS = [
    {"to": "010-1111-1111", "name": "가", "var1": "1기", "var2": "x"},
    {"to": "010-2222-2222", "name": "나", "var1": "2기", "var2": "y"},
]


ROSTER_HEADER = ["연번", "성명", "휴대폰"]


@pytest.fixture
def ws(monkeypatch) -> FakeWorksheet:
    """ROWS 두 명이 올라 있는 명단 탭. 어느 탭을 열었는지도 기록한다."""
    sheet = FakeWorksheet(
        [
            ROSTER_HEADER,
            ["1", "가", "010-1111-1111"],
            ["2", "나", "010-2222-2222"],
        ]
    )

    def fake_open(spreadsheet_id, worksheet=None, gid=None):
        sheet.opened = {"id": spreadsheet_id, "worksheet": worksheet, "gid": gid}
        return sheet

    monkeypatch.setattr(ledger, "open_roster", fake_open)
    return sheet


def _kst_now() -> datetime.datetime:
    """사람이 예약 시각을 적을 때 보는 벽시계.

    naive now() 를 쓰면 컨테이너(UTC)와 KST 로 판정하는 코드가 9시간 어긋나
    로컬(KST)에서만 통과한다. 그 결합이 사라지면 테스트가 아무것도 못 잡는다.
    """
    return datetime.datetime.now(KST).replace(tzinfo=None)


def _already(ws, phone: str, value: str = "2026-08-05 10:00") -> None:
    """그 사람은 이 캠페인을 이미 받은 것으로 표시한다."""
    ledger.mark(ws, "discord", [phone], value)


def _campaign(ws, name: str = "discord") -> int:
    """캠페인 열 번호. 테스트가 열 위치를 가정하지 않게 한다."""
    return ws.rows[0].index(name)


def _run(monkeypatch, response=None, boom=None, **extra):
    captured = {}

    def fake_send(payload):
        captured.update(payload)
        if boom:
            raise boom
        return response

    monkeypatch.setattr(transport, "send", fake_send)
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        template_name="discord",
        rows=ROWS,
        requested_by="a@team-mono.com",
        entrypoint="slack",
        **extra,
    )
    return result, captured


def test_보내기_전에_자리를_잡는다(ws, monkeypatch):
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    # 명단 두 사람 모두 캠페인 열이 채워진다.
    at = _campaign(ws)
    assert "" not in (ws.rows[1][at], ws.rows[2][at])
    assert result["sent"] == 2
    assert [t["to"] for t in payload["targets"]] == ["01011111111", "01022222222"]


def test_지정한_탭을_연다(ws, monkeypatch):
    # 주소에 gid 가 있는데 첫 탭을 열면, 명단이 두 번째 탭인 시트에서
    # 엉뚱한 탭에 캠페인 열을 만들고 아무도 모른다.
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"}, gid=1077887383)

    assert ws.opened["gid"] == 1077887383


def test_보내고_나면_선점_표시가_일시로_바뀐다(ws, monkeypatch):
    _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    at = _campaign(ws)
    assert not ws.rows[1][at].startswith(ledger.SENDING)
    assert ws.rows[1][at]


def test_이미_보낸_번호는_대상에서_빠진다(ws, monkeypatch):
    _already(ws, "01011111111")
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1 and result["skipped"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]
    # 도달 확인은 이 목록만 기다린다.
    assert result["sent_to"] == ["01022222222"]


def test_사람이_손으로_적은_줄도_존중한다(ws, monkeypatch):
    # 서버가 죽어 뿌리오 웹으로 직접 보낸 뒤 손으로 남긴 기록. 사람은 번호를
    # 하이픈까지 넣어 적는다 — 그 표기로도 대조돼야 두 번 가지 않는다.
    _already(ws, "01011111111", "수기 발송")
    result, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K1"})

    assert result["sent"] == 1
    assert [t["to"] for t in payload["targets"]] == ["01022222222"]


def test_전원_중복이면_벤더를_부르지_않는다(ws, monkeypatch):
    _already(ws, "01011111111")
    _already(ws, "01022222222")
    result, payload = _run(monkeypatch, {"code": "1000"})

    assert result["sent"] == 0 and result["skipped"] == 2
    assert payload == {}


def test_명단에_없는_번호는_보내지_않고_알린다(ws, monkeypatch):
    # 조용히 빼면 안 간 줄 모르고, 그냥 보내면 기록할 곳이 없다.
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=[{"to": "010-1111-1111"}, {"to": "010-9999-9999"}],
        content="[*이름*]님",
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )

    assert result["sent"] == 1
    assert result["missing"] == ["01099999999"]


def test_벤더가_거절하면_선점을_풀어_재시도를_연다(ws, monkeypatch):
    # 4xx 는 접수되지 않은 것이 확실하다. 그때만 재시도를 연다.
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, boom=transport.PpurioError(400, "bad request"))

    at = _campaign(ws)
    assert [ws.rows[1][at], ws.rows[2][at]] == ["", ""]


@pytest.mark.parametrize(
    "boom",
    [
        TimeoutError("read timed out"),
        urllib.error.HTTPError("u", 504, "gateway timeout", {}, None),
        ConnectionResetError("peer reset"),
    ],
    ids=["timeout", "504", "reset"],
)
def test_접수_여부를_모르면_선점을_남겨_재시도를_막는다(ws, monkeypatch, boom):
    # 벤더가 이미 접수하고 응답만 못 돌려줬을 수 있다. 선점을 풀면 다음 시도가
    # 빈 칸을 보고 같은 사람에게 두 번 보낸다. 504 는 게이트웨이가 대신
    # 돌려주는 타임아웃이라 소켓 타임아웃과 같은 취급이어야 한다.
    with pytest.raises(type(boom)):
        _run(monkeypatch, boom=boom)

    at = _campaign(ws)
    assert ws.rows[1][at].startswith(ledger.SENDING)


def test_접수코드가_1000이_아니면_실패로_본다(ws, monkeypatch):
    with pytest.raises(transport.PpurioError):
        _run(monkeypatch, {"code": "2000", "description": "invalid"})

    at = _campaign(ws)
    assert ws.rows[1][at] == ""


def test_벤더_옵션은_그대로_통과한다(ws, monkeypatch):
    at = _kst_now() + datetime.timedelta(hours=1)
    _, payload = _run(
        monkeypatch,
        {"code": "1000", "messageKey": "K"},
        send_at=at,
        duplicateFlag="N",
    )
    # transport 가 모르는 필드도 벤더로 흘러간다.
    assert payload["duplicateFlag"] == "N"
    assert payload["refKey"] == "discord"


def test_LMS면_제목을_붙인다(ws, monkeypatch):
    long_body = "가" * 100  # EUC-KR 200byte — SMS 90byte 한도를 넘는다
    monkeypatch.setattr(
        transport, "send", lambda payload: {"code": "1000", "messageKey": "K"}
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=ROWS,
        content=long_body,
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    assert result["message_type"] == "LMS"


def test_즉석_문안도_보낼_수_있다(ws, monkeypatch):
    # 저장된 문안 파일 없이 이번에만 쓰는 본문. 이 경로가 사라지면
    # 급한 공지를 파일부터 만들어야 보낼 수 있게 된다.
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="adhoc",
        rows=ROWS,
        content="[*이름*]선생님, 오늘 일정이 변경되었습니다.",
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    assert result["sent"] == 2
    assert captured["content"].startswith("[*이름*]선생님")


def test_같은_번호가_두_번_들어와도_한_번만_보낸다(ws, monkeypatch):
    # 접지 않으면 claim 이 한 번호에 두 행을 만들고, 승자 행의 주인이 사라져
    # 그 번호는 이 캠페인에서 영영 발송되지 않는다.
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    result = sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=[
            {"to": "010-1111-1111", "name": "가"},
            {"to": "01011111111", "name": "가(표기만 다름)"},
        ],
        content="[*이름*]님",
        requested_by="a@team-mono.com",
        entrypoint="slack",
    )
    assert result["sent"] == 1
    assert [t["to"] for t in captured["targets"]] == ["01011111111"]


def test_예약이_3분보다_가까우면_시트를_건드리기_전에_막는다(ws, monkeypatch):
    # 벤더도 거부하지만 그 거부는 발송 시도 뒤에야 돌아온다. 그때는 이미
    # 자리를 잡은 뒤라 재시도가 막힌다.
    soon = _kst_now() + datetime.timedelta(minutes=1)
    with pytest.raises(ValueError, match="3분"):
        _run(monkeypatch, {"code": "1000"}, send_at=soon)

    # 시트에 캠페인 열조차 생기지 않는다.
    assert ws.rows[0] == ROSTER_HEADER


def test_예약이면_sendTime이_실린다(ws, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        transport,
        "send",
        lambda payload: captured.update(payload) or {"code": "1000", "messageKey": "K"},
    )
    at = _kst_now() + datetime.timedelta(hours=1)
    sms_send.send_campaign(
        spreadsheet_id="S1",
        campaign="discord",
        rows=ROWS,
        content="[*이름*]님",
        requested_by="a@team-mono.com",
        entrypoint="script",
        send_at=at,
    )
    # 뿌리오 sendTime 은 yyyy-MM-ddTHH:mm:ss 다.
    assert captured["sendTime"] == at.strftime("%Y-%m-%dT%H:%M:%S")


def test_예약_판정은_컨테이너_시계가_UTC여도_KST로_한다(monkeypatch):
    # 컨테이너에 TZ 가 없어 datetime.now() 는 UTC 다. 사람은 KST 벽시계로
    # 적으므로, 그대로 빼면 이미 지난 시각이 9시간 여유로 보여 통과한다.
    import zoneinfo

    now_kst = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Seoul")).replace(
        tzinfo=None
    )
    past = now_kst - datetime.timedelta(hours=1)
    with pytest.raises(ValueError, match="3분"):
        sms_send.reserve_time(past)


def test_문제를_한_번에_모아_돌려준다():
    # 하나씩 터뜨리면 고치고 다시 돌리고를 반복하게 된다.
    soon = _kst_now() + datetime.timedelta(minutes=1)
    problems = sms_send.check(
        [{"to": "010-123"}, {"to": "010-456"}],
        template_name="discord",
        send_at=soon,
    )
    assert sum("형식 오류" in p for p in problems) == 2
    assert any("3분" in p for p in problems)


def test_check가_막는_것과_send가_막는_것이_같다(ws, monkeypatch):
    # check 가 통과시킨 발송이 send_campaign 에서 터지면 안 된다. 검증이
    # 두 벌이면 사람에게 보여준 것과 실제로 막히는 것이 갈라진다.
    bad = [{"to": "010-123"}]
    assert sms_send.check(bad, template_name="discord")
    with pytest.raises(ValueError):
        sms_send.send_campaign(
            spreadsheet_id="S1",
            campaign="x",
            rows=bad,
            template_name="discord",
            requested_by="a@team-mono.com",
            entrypoint="slack",
        )


def test_중복은_차단_사유가_아니다():
    # 접어서 보내므로 발송은 된다. 대신 preview 가 몇 건 접었는지 센다.
    rows = [{"to": "010-1111-1111"}, {"to": "01011111111"}]
    assert sms_send.check(rows, template_name="discord") == []
    assert sms_send.preview(rows, "discord")["folded"] == 1


def test_문안이_없으면_거기서_멈춘다():
    # 문안이 없으면 길이도 치환도 볼 수 없다.
    assert sms_send.check(ROWS, template_name="없는문안") == [
        "문안 '없는문안' 없음. 사용 가능: discord"
    ]


def test_문안과_즉석본문을_동시에_주면_거른다():
    assert sms_send.check(ROWS, template_name="discord", content="둘 다")


def test_수신자가_없으면_거른다():
    assert sms_send.check([], template_name="discord") == ["수신자가 없습니다."]


def test_통과하면_빈_목록이다():
    assert sms_send.check(ROWS, template_name="discord") == []


def test_수신자가_없으면_시트를_건드리지_않는다(ws, monkeypatch):
    # check 는 막는데 send 가 통과시키면, 벤더도 안 부른 실행이 운영 시트에
    # 빈 캠페인 열만 남긴다.
    monkeypatch.setattr(transport, "send", lambda payload: {"code": "1000"})

    with pytest.raises(ValueError, match="수신자가 없습니다"):
        sms_send.send_campaign(
            spreadsheet_id="S1",
            campaign="discord",
            rows=[],
            content="본문",
            requested_by="a@team-mono.com",
            entrypoint="slack",
        )

    assert ws.rows[0] == ROSTER_HEADER


def test_예약이면_나갈_시각을_적는다(ws, monkeypatch):
    # 접수 시각을 적으면 아직 안 나간 문자가 "그날 발송됨"으로 읽힌다.
    at_time = _kst_now() + datetime.timedelta(hours=3)
    _run(monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=at_time)

    assert ws.rows[1][_campaign(ws)] == f"예약 {at_time.strftime('%Y-%m-%d %H:%M')}"


def test_오프셋을_붙여도_시트와_벤더가_같은_시각을_본다(ws, monkeypatch):
    # 한쪽만 KST 로 눕히면 시트에 적힌 시각과 실제 발송 시각이 9시간 어긋난다.
    aware = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    _, payload = _run(monkeypatch, {"code": "1000", "messageKey": "K"}, send_at=aware)

    kst = aware.astimezone(KST).replace(tzinfo=None)
    assert payload["sendTime"] == kst.strftime("%Y-%m-%dT%H:%M:%S")
    assert ws.rows[1][_campaign(ws)] == f"예약 {kst.strftime('%Y-%m-%d %H:%M')}"


def test_취소가_거절되면_성공으로_보지_않는다(monkeypatch):
    # 응답을 안 보면 "1분 전 초과"로 거절당한 취소가 성공처럼 출력되고,
    # 사람은 취소된 줄 알고 자리를 뜨는데 문자는 나간다.
    monkeypatch.setattr(
        transport, "cancel", lambda key: {"code": "7000", "description": "too late"}
    )

    with pytest.raises(transport.PpurioError):
        sms_send.cancel_reserved("K1")


def test_발송중으로_막힌_사람은_따로_알린다(ws, monkeypatch):
    # 끝난 것과 섞으면 접수도 안 된 캠페인을 끝난 것으로 알고 넘어간다.
    ledger.mark(ws, "discord", ["01011111111"], f"{ledger.SENDING} 2026-08-05 10:00")
    result, _ = _run(monkeypatch, {"code": "1000", "messageKey": "K"})

    assert result["blocked"] == ["01011111111"]
    assert result["skipped"] == 0
    assert result["sent"] == 1
