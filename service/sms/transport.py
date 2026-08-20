"""
뿌리오 HTTP 계층입니다.

이 모듈은 벤더 payload 를 해석하지 않습니다. 인증(계정·발신번호·토큰)만
채워 넣고 나머지는 그대로 흘려보냅니다.

호출 IP 는 뿌리오에 사전 등록되어야 합니다. 등록되지 않은 곳에서 부르면
토큰 발급 단계에서 code 3003(invalid ip)으로 막힙니다.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any

BASE = "https://message.ppurio.com"
TIMEOUT = 20


class PpurioError(RuntimeError):
    """접수되지 않은 것이 확실할 때 발생합니다.

    4xx, 200 이면서 code 가 성공이 아닌 경우, 그리고 인증 설정이 없어 요청이
    이 머신을 떠나지도 못한 경우입니다. 셋 다 "안 나갔다"가 확실합니다.

    접수 여부를 모르는 경우(타임아웃·5xx)는 이 예외가 아닙니다 — 원래 예외
    그대로 올라가고, 사람이 뿌리오 웹에서 확인해야 합니다.

    status 0 은 요청이 나가지 않았다는 뜻입니다.
    """

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"뿌리오 응답 [{status}] {body}")


def _env(name: str) -> str:
    """뿌리오 인증 설정을 환경변수에서 읽습니다.

    없으면 PpurioError 로 바꿔 던집니다. KeyError 로 새어 나가면 호출부의
    `except PpurioError` 를 우회해, 설정 누락이 벤더 거절과 구분되지 않습니다.

    Args:
        name: PPURIO_ACCOUNT · PPURIO_API_KEY · PPURIO_SENDER

    Returns:
        str: 환경변수 값

    Raises:
        PpurioError: 환경변수가 없을 때
    """
    try:
        return os.environ[name]
    except KeyError as error:
        raise PpurioError(0, f"환경변수 {name} 가 없습니다") from error


def _post(path: str, body: dict, headers: dict) -> dict:
    """뿌리오에 POST 합니다.

    Args:
        path: /v1/message 같은 경로
        body: 요청 본문
        headers: Authorization 을 포함한 헤더

    Returns:
        dict: 응답 본문

    Raises:
        PpurioError: 4xx 일 때 (거절이 확실하다)
        urllib.error.HTTPError: 5xx 일 때 (접수 여부를 모른다)
    """
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode(),
        method="POST",
        headers={"Content-Type": "application/json;charset=utf-8", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # 504 는 게이트웨이가 대신 돌려주는 타임아웃이라, 뿌리오가 이미
        # 접수하고 응답만 못 온 경우와 구분되지 않는다. "안 나갔다"로 단정하면
        # 안 된다.
        if error.code >= 500:
            raise
        # 국내 벤더가 EUC-KR 로 오류 본문을 주면 UnicodeDecodeError 가 나
        # PpurioError 를 비켜간다. 본문은 사람이 읽을 텍스트일 뿐이다.
        body = error.read().decode(errors="replace")[:600]
        raise PpurioError(error.code, body) from error


def issue_token() -> str:
    """액세스 토큰을 발급받습니다.

    실패는 종류를 가리지 않고 PpurioError 로 바꿉니다. "토큰 단계에서 터졌으면
    /v1/message 는 만들어지지도 않았다"가 종류와 무관하게 참이기 때문입니다.
    프록시가 200 에 HTML 이나 EUC-KR 을 실어 주면 JSONDecodeError·
    UnicodeDecodeError 가 나는데, 열거로 잡으면 그것들이 새어 나가 호출부의
    except PpurioError 를 비켜갑니다.

    조용히 삼키지 않습니다. 사유를 담아 타입만 바꿔 다시 던집니다.

    Returns:
        str: Bearer 토큰

    Raises:
        PpurioError: 발급에 실패했을 때
    """
    pair = f'{_env("PPURIO_ACCOUNT")}:{_env("PPURIO_API_KEY")}'
    basic = base64.b64encode(pair.encode()).decode()
    try:
        result = _post("/v1/token", {}, {"Authorization": "Basic " + basic})
    except PpurioError:
        raise
    except Exception as error:
        raise PpurioError(0, f"토큰 발급 실패: {error}") from error
    if "token" not in result:
        raise PpurioError(200, result)
    return result["token"]


def send(payload: dict) -> dict:
    """메시지를 발송합니다. payload 는 벤더 스펙 그대로입니다.

    account·from 은 여기서 정합니다. 호출부가 발신번호를 덮을 수 없습니다.
    messageType·content·targets 는 준 값을 그대로 보냅니다.

    Args:
        payload: 뿌리오 /v1/message 요청 본문 (account 제외)

    Returns:
        dict: 뿌리오 응답. code 가 "1000" 이면 접수 성공

    Raises:
        PpurioError: HTTP 실패
    """
    body = {
        **payload,
        "account": _env("PPURIO_ACCOUNT"),
        "from": _env("PPURIO_SENDER"),
    }
    return _post("/v1/message", body, {"Authorization": "Bearer " + issue_token()})
