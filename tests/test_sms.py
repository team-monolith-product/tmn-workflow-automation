"""
문자 발송(뿌리오) 서비스 테스트
"""

import pytest

from service import ppurio_result, sms


async def _noop_progress(text: str) -> None:
    """진행 보고를 무시하는 콜백"""


def test_message_type_uses_euckr_bytes():
    """한글 45자(EUC-KR 90바이트)까지는 SMS, 넘으면 LMS입니다."""
    assert sms.message_type("가" * 45) == "SMS"
    assert sms.message_type("가" * 46) == "LMS"


def test_normalize_phone_strips_separators():
    """하이픈·공백은 제거하고 숫자만 남깁니다."""
    assert sms.normalize_phone("010-1234-5678") == "01012345678"
    assert sms.normalize_phone(" 010 1234 5678 ") == "01012345678"


def test_normalize_phone_rejects_invalid():
    """자릿수가 맞지 않으면 발송 전에 걸러냅니다."""
    with pytest.raises(ValueError):
        sms.normalize_phone("010-123")


def test_render_content_replaces_name():
    """{name} 은 수신자 이름으로 치환됩니다."""
    assert (
        sms.render_content("{name}선생님 안녕하세요", "홍길동")
        == "홍길동선생님 안녕하세요"
    )


def test_build_payload_omits_subject_for_sms(monkeypatch):
    """SMS 에는 제목이 들어가지 않고, LMS 에만 붙습니다."""
    monkeypatch.setenv("PPURIO_ACCOUNT", "teammono")
    monkeypatch.setenv("PPURIO_SENDER", "01000000000")

    short = sms.build_payload("01012345678", "짧은 문자", "제목", "ref-1")
    assert short["messageType"] == "SMS"
    assert "subject" not in short

    long_payload = sms.build_payload("01012345678", "가" * 100, "제목", "ref-1")
    assert long_payload["messageType"] == "LMS"
    assert long_payload["subject"] == "제목"


def test_parse_results_reads_phone_and_status_per_row():
    """발송결과 표에서 행 단위로 번호와 상태를 뽑습니다."""
    html = """
    <table>
      <tr><th>수신번호</th><th>결과</th></tr>
      <tr><td>010-1111-2222</td><td>성공</td></tr>
      <tr><td>01033334444</td><td>발송실패</td></tr>
      <tr><td>010 5555 6666</td><td>전송중</td></tr>
    </table>
    """
    assert ppurio_result.parse_results(html) == {
        "01011112222": ppurio_result.DELIVERED,
        "01033334444": ppurio_result.FAILED,
        "01055556666": ppurio_result.PENDING,
    }


@pytest.fixture
def no_wait(monkeypatch):
    """폴링 대기 시간을 없애 테스트를 즉시 진행시킵니다."""
    monkeypatch.setattr(sms, "POLL_INTERVAL_SECONDS", 0)


def _accept_all(recipients, template, subject, ref_prefix):
    """모든 수신자를 접수 성공으로 처리하는 가짜 발송 함수"""
    return [
        {
            "name": recipient["name"],
            "phone": recipient["phone"],
            "code": "1000",
            "description": "정상",
            "message_key": "key",
        }
        for recipient in recipients
    ]


async def test_send_and_confirm_retries_only_failures(monkeypatch, no_wait):
    """실패한 번호만 재발송하고, 모두 성공하면 멈춥니다."""
    sent_rounds = []

    def fake_send(recipients, template, subject, ref_prefix):
        sent_rounds.append([recipient["phone"] for recipient in recipients])
        return _accept_all(recipients, template, subject, ref_prefix)

    statuses = [
        {"01011112222": "성공", "01033334444": "실패"},
        {"01033334444": "성공"},
    ]

    async def fake_fetch(phones):
        return statuses.pop(0)

    monkeypatch.setattr(sms, "send_messages", fake_send)
    monkeypatch.setattr(ppurio_result, "fetch_results", fake_fetch)

    report = await sms.send_and_confirm(
        [
            {"name": "가", "phone": "010-1111-2222"},
            {"name": "나", "phone": "010-3333-4444"},
        ],
        "{name}님 안녕하세요",
        "안내",
        _noop_progress,
    )

    assert sent_rounds == [["01011112222", "01033334444"], ["01033334444"]]
    assert len(report["delivered"]) == 2
    assert report["failed"] == []
    assert report["rounds"] == 2


async def test_send_and_confirm_does_not_resend_unconfirmed(monkeypatch, no_wait):
    """결과가 '대기'인 건은 중복 발송을 피하려 재발송하지 않습니다."""
    send_count = 0

    def fake_send(recipients, template, subject, ref_prefix):
        nonlocal send_count
        send_count += 1
        return _accept_all(recipients, template, subject, ref_prefix)

    async def fake_fetch(phones):
        return {phone: "대기" for phone in phones}

    monkeypatch.setattr(sms, "send_messages", fake_send)
    monkeypatch.setattr(ppurio_result, "fetch_results", fake_fetch)

    report = await sms.send_and_confirm(
        [{"name": "가", "phone": "010-1111-2222"}], "본문", "안내", _noop_progress
    )

    assert send_count == 1
    assert report["delivered"] == []
    assert len(report["unknown"]) == 1


async def test_send_and_confirm_retries_rejected_acceptance(monkeypatch, no_wait):
    """접수 자체가 거절된 건은 문자가 나가지 않았으므로 재발송합니다."""
    attempts = []

    def fake_send(recipients, template, subject, ref_prefix):
        attempts.append(len(recipients))
        return [
            {
                "name": recipient["name"],
                "phone": recipient["phone"],
                "code": "3003",
                "description": "invalid ip",
                "message_key": None,
            }
            for recipient in recipients
        ]

    async def fake_fetch(phones):
        return {}

    monkeypatch.setattr(sms, "send_messages", fake_send)
    monkeypatch.setattr(ppurio_result, "fetch_results", fake_fetch)

    report = await sms.send_and_confirm(
        [{"name": "가", "phone": "010-1111-2222"}], "본문", "안내", _noop_progress
    )

    assert attempts == [1] * sms.SEND_ROUNDS
    assert len(report["failed"]) == 1
    assert "invalid ip" in report["failed"][0]["reason"]


def test_format_report_lists_failures():
    """최종 보고에는 실패·미확정 건이 이유와 함께 나옵니다."""
    text = sms.format_report(
        {
            "delivered": [{"name": "가", "phone": "01011112222"}],
            "failed": [
                {"name": "나", "phone": "01033334444", "reason": "발송 실패"},
            ],
            "unknown": [{"name": "다", "phone": "01055556666"}],
            "rounds": 3,
        }
    )
    assert "성공 1건" in text
    assert "나 01033334444 — 발송 실패" in text
    assert "01055556666" in text
