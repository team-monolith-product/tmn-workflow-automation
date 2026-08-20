"""전송 계층 테스트.

발송 테스트는 transport.send 를 통째로 목킹하므로 이 계층이 벤더에 무엇을
보내는지 아무도 검증하지 않았다. 발신번호가 빠져도 전 케이스가 초록이었고,
실계정에서만 100% 거절된다.
"""

import io
import json
import urllib.error
import urllib.request

import pytest

from service.sms import transport


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PPURIO_ACCOUNT", "acct")
    monkeypatch.setenv("PPURIO_API_KEY", "key")
    monkeypatch.setenv("PPURIO_SENDER", "025770000")


@pytest.fixture
def posted(monkeypatch):
    """_post 로 나가는 본문을 잡아둔다."""
    sent = {}

    def fake_post(path, body, headers):
        sent["path"] = path
        sent["body"] = body
        return {"code": "1000", "messageKey": "K"}

    monkeypatch.setattr(transport, "_post", fake_post)
    monkeypatch.setattr(transport, "issue_token", lambda: "TOKEN")
    return sent


def test_계정과_발신번호를_채워_보낸다(env, posted):
    # 발신번호 사전등록제라 from 없이는 접수되지 않는다.
    transport.send({"messageType": "LMS", "content": "안녕"})

    assert posted["body"]["account"] == "acct"
    assert posted["body"]["from"] == "025770000"
    assert posted["body"]["content"] == "안녕"


def test_호출부가_준_값이_이긴다(env, posted):
    # 벤더 옵션 통과 계약. 우리가 채운 기본값을 호출부가 덮을 수 있어야 한다.
    transport.send({"from": "0316000000"})

    assert posted["body"]["from"] == "0316000000"


def test_인증_설정이_없으면_PpurioError로_거절한다(monkeypatch):
    # KeyError 로 새어 나가면 호출부의 except PpurioError 를 우회해, 이력에
    # 자리는 잡힌 채 접수코드가 빈 행으로 남는다. 그 행은 살아 있는 것으로
    # 취급되어 환경변수를 채워 다시 돌려도 한 통도 안 나간다.
    monkeypatch.delenv("PPURIO_ACCOUNT", raising=False)

    with pytest.raises(transport.PpurioError):
        transport.send({"content": "안녕"})


@pytest.mark.parametrize(
    "boom",
    [
        TimeoutError("read timed out"),
        urllib.error.URLError("unreachable"),
        # urlopen 이 getresponse() 에서 내는 것은 URLError 로 감싸이지 않는다.
        ConnectionResetError("peer reset"),
        # 프록시가 200 에 HTML 이나 EUC-KR 을 실어 주는 경우.
        json.JSONDecodeError("Expecting value", "<html>", 0),
        UnicodeDecodeError("utf-8", b"\xb0\xa1", 0, 1, "invalid start byte"),
    ],
)
def test_토큰_단계_실패는_종류를_가리지_않고_안_나간_것으로_본다(
    env, monkeypatch, boom
):
    # 종류를 열거해 잡으면 빠진 것이 새어 나가 호출부의 except PpurioError 를
    # 비켜가고, 접수코드가 빈 행이 남아 campaign 이 영구히 잠긴다.
    # "토큰 단계에서 터졌으면 /v1/message 는 만들어지지도 않았다"는 종류와
    # 무관하게 참이라, 여기서는 넓게 잡는 쪽이 정확하다.
    paths = []

    def fake_post(path, body, headers):
        paths.append(path)
        raise boom

    monkeypatch.setattr(transport, "_post", fake_post)

    with pytest.raises(transport.PpurioError):
        transport.send({"content": "안녕"})

    # /v1/message 는 만들어지지도 않았다 — 그래서 안 나간 것이 확실하다.
    assert paths == ["/v1/token"]


def _raise_http(status: int):
    """urlopen 이 그 상태 코드로 터지게 만든다."""

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, status, "boom", {}, io.BytesIO(b'{"code":"9999"}')
        )

    return fake_urlopen


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx는_PpurioError로_바꾸지_않는다(env, monkeypatch, status):
    # PpurioError 는 호출부가 선점을 푸는 신호다. 504 는 게이트웨이가 대신
    # 돌려주는 타임아웃이라 뿌리오가 이미 접수했을 수 있는데, 여기서 바꾸면
    # 재시도가 열려 같은 사람에게 두 번 나간다.
    monkeypatch.setattr(urllib.request, "urlopen", _raise_http(status))

    with pytest.raises(urllib.error.HTTPError):
        transport._post("/v1/message", {}, {})


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_4xx는_안_나간_것이_확실하다(env, monkeypatch, status):
    monkeypatch.setattr(urllib.request, "urlopen", _raise_http(status))

    with pytest.raises(transport.PpurioError):
        transport._post("/v1/message", {}, {})


def test_토큰_단계의_5xx는_안_나간_것으로_본다(env, monkeypatch):
    # 토큰이 없으면 /v1/message 는 만들어지지도 않는다. 같은 5xx 라도
    # 토큰 단계는 "안 나갔다"가 확실하므로 여기서는 바꾸는 것이 맞다.
    monkeypatch.setattr(urllib.request, "urlopen", _raise_http(503))

    with pytest.raises(transport.PpurioError):
        transport.issue_token()


def test_발신번호가_없어도_PpurioError로_거절한다(monkeypatch):
    monkeypatch.setenv("PPURIO_ACCOUNT", "acct")
    monkeypatch.setenv("PPURIO_API_KEY", "key")
    monkeypatch.delenv("PPURIO_SENDER", raising=False)

    with pytest.raises(transport.PpurioError):
        transport.send({"content": "안녕"})
