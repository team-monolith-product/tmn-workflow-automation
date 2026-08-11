"""전송 계층 테스트.

발송 테스트는 transport.send 를 통째로 목킹하므로 이 계층이 벤더에 무엇을
보내는지 아무도 검증하지 않았다. 발신번호가 빠져도 전 케이스가 초록이었고,
실계정에서만 100% 거절된다.
"""

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


def test_발신번호가_없어도_PpurioError로_거절한다(monkeypatch):
    monkeypatch.setenv("PPURIO_ACCOUNT", "acct")
    monkeypatch.setenv("PPURIO_API_KEY", "key")
    monkeypatch.delenv("PPURIO_SENDER", raising=False)

    with pytest.raises(transport.PpurioError):
        transport.send({"content": "안녕"})
