"""
뿌리오 HTTP 계층입니다.

이 모듈은 벤더 payload 를 해석하지 않습니다. 인증(계정·토큰)만 채워 넣고
나머지는 그대로 흘려보냅니다. 벤더가 필드를 추가해도, 예약 발송처럼 우리가
아직 안 쓰는 기능을 쓰게 돼도 여기는 고치지 않습니다.

앞선 구현(industry-linked/ppurio.py)은 targetCount 를 1 로 못박아 두어서
벤더가 제공하는 다중 수신자와 changeWord 치환을 쓸 수 없었고, 그 때문에
명단 엑셀을 만들어 사람이 웹에 올리는 경로가 따로 생겼습니다. 통과시키지
않으면 벤더 기능을 잃습니다.

비즈뿌리오로 옮길 때 새로 쓰는 파일은 여기 하나입니다.

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
    """뿌리오가 실패를 돌려줬을 때 발생합니다."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"뿌리오 응답 [{status}] {body}")


def _credentials() -> tuple[str, str]:
    """계정과 인증키를 환경변수에서 읽습니다.

    Returns:
        tuple[str, str]: (계정, 인증키)
    """
    return os.environ["PPURIO_ACCOUNT"], os.environ["PPURIO_API_KEY"]


def _post(path: str, body: dict, headers: dict) -> dict:
    """뿌리오에 POST 합니다.

    Args:
        path: /v1/message 같은 경로
        body: 요청 본문
        headers: Authorization 을 포함한 헤더

    Returns:
        dict: 응답 본문

    Raises:
        PpurioError: 2xx 가 아니거나 본문이 JSON 이 아닐 때
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
        raise PpurioError(error.code, error.read().decode()[:600]) from error


def issue_token() -> str:
    """액세스 토큰을 발급받습니다.

    Returns:
        str: Bearer 토큰

    Raises:
        PpurioError: 발급에 실패했을 때
    """
    account, key = _credentials()
    basic = base64.b64encode(f"{account}:{key}".encode()).decode()
    result = _post("/v1/token", {}, {"Authorization": "Basic " + basic})
    if "token" not in result:
        raise PpurioError(200, result)
    return result["token"]


def send(payload: dict, token: str | None = None) -> dict:
    """메시지를 발송합니다. payload 는 벤더 스펙 그대로입니다.

    account 만 채워 넣습니다. messageType·content·targets·sendTime 등은
    호출부가 준 값을 그대로 보냅니다.

    Args:
        payload: 뿌리오 /v1/message 요청 본문 (account 제외)
        token: 재사용할 토큰. 생략하면 새로 발급합니다

    Returns:
        dict: 뿌리오 응답. code 가 "1000" 이면 접수 성공

    Raises:
        PpurioError: HTTP 실패
    """
    account, _ = _credentials()
    return _post(
        "/v1/message",
        {"account": account, **payload},
        {"Authorization": "Bearer " + (token or issue_token())},
    )


def cancel(message_key: str, token: str | None = None) -> dict:
    """예약 발송을 취소합니다. 발송 1분 전까지만 가능합니다.

    Args:
        message_key: 접수 시 받은 messageKey
        token: 재사용할 토큰

    Returns:
        dict: 뿌리오 응답
    """
    account, _ = _credentials()
    return _post(
        "/v1/cancel",
        {"account": account, "messageKey": message_key},
        {"Authorization": "Bearer " + (token or issue_token())},
    )
