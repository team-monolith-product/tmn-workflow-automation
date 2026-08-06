"""노션 전송 계층이 어떤 응답을 다시 치는지 검증합니다."""

import httpx
import pytest

from service import notion_transport
from service.notion_transport import ThrottledTransport


@pytest.fixture
def no_wait(monkeypatch):
    """대기를 없애고 잔 시간을 모읍니다.

    Returns:
        list[float]: time.sleep에 넘어간 시간
    """
    slept: list[float] = []
    monkeypatch.setattr(notion_transport.time, "sleep", slept.append)
    monkeypatch.setattr(ThrottledTransport, "_wait_turn", lambda self: None)
    return slept


def _responding(monkeypatch, statuses: list[int], headers: dict | None = None) -> list:
    """정해진 상태를 차례로 돌려주도록 바꿉니다.

    Args:
        monkeypatch: pytest monkeypatch
        statuses: 돌려줄 상태 코드
        headers: 응답에 실을 헤더

    Returns:
        list: 실제로 보낸 요청
    """
    sent = []

    def handle(self, request):
        sent.append(request)
        return httpx.Response(statuses[len(sent) - 1], headers=headers or {})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return sent


def test_504는_다시_친다(monkeypatch, no_wait):
    """열거 도중의 게이트웨이 오류는 회차를 끝내지 않고 다시 칩니다."""
    sent = _responding(monkeypatch, [504, 200])
    transport = ThrottledTransport()

    response = transport.handle_request(httpx.Request("GET", "https://api.notion.com"))

    assert response.status_code == 200
    assert len(sent) == 2


def test_429는_Retry_After만큼_기다린다(monkeypatch, no_wait):
    """한도 초과는 노션이 알려준 시간을 그대로 씁니다."""
    _responding(monkeypatch, [429, 200], {"Retry-After": "3"})
    transport = ThrottledTransport()

    transport.handle_request(httpx.Request("GET", "https://api.notion.com"))

    assert no_wait == [3.0]


def test_Retry_After가_없으면_배로_늘린다(monkeypatch, no_wait):
    """5xx에는 이 헤더가 없어 같은 간격으로는 5초 만에 다 씁니다."""
    _responding(monkeypatch, [503, 503, 503, 200])
    transport = ThrottledTransport()

    transport.handle_request(httpx.Request("GET", "https://api.notion.com"))

    assert no_wait == [1.0, 2.0, 4.0]


def test_404는_그대로_돌려준다(monkeypatch, no_wait):
    """권한 밖은 다시 쳐도 같은 답이라 한 번만 부릅니다."""
    sent = _responding(monkeypatch, [404])
    transport = ThrottledTransport()

    response = transport.handle_request(httpx.Request("GET", "https://api.notion.com"))

    assert response.status_code == 404
    assert len(sent) == 1


def test_재시도를_다_쓰면_마지막_응답을_돌려준다(monkeypatch, no_wait):
    """삼키지 않습니다. 호출자가 상태를 보고 올립니다."""
    sent = _responding(monkeypatch, [504] * 6)
    transport = ThrottledTransport(max_retries=5)

    response = transport.handle_request(httpx.Request("GET", "https://api.notion.com"))

    assert response.status_code == 504
    assert len(sent) == 6
