"""
뿌리오 HTTP 계층입니다.

이 모듈은 벤더 payload 를 해석하지 않습니다. 인증(계정·토큰)만 채워 넣고
나머지는 그대로 흘려보냅니다. 벤더가 필드를 추가해도, 예약 발송처럼 우리가
아직 안 쓰는 기능을 쓰게 돼도 여기는 고치지 않습니다.

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

    HTTP 오류, 200 이면서 code 가 성공이 아닌 경우, 그리고 인증 설정이 없어
    요청이 이 머신을 떠나지도 못한 경우입니다. 셋 다 '보내지 않았다'가 확실해
    호출부가 재시도를 열어도 됩니다. 타임아웃처럼 접수 여부를 모르는 경우는
    이 예외가 아닙니다 — 그건 접수코드를 비운 채 전파돼 사람이 확인합니다.

    status 0 은 요청이 나가지 않았다는 뜻입니다.
    """

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"뿌리오 응답 [{status}] {body}")


def _credentials() -> tuple[str, str]:
    """계정과 인증키를 환경변수에서 읽습니다.

    없으면 PpurioError 로 바꿔 던집니다. KeyError 로 새어 나가면 호출부의
    `except PpurioError` 를 우회해, 이력 시트에 자리는 잡힌 채 접수코드가 빈
    행으로 남습니다. 빈 칸은 "보냈는지 모름"이라 살아 있는 것으로 취급되므로
    환경변수를 채워 다시 돌려도 "모두 이미 발송된 상태"라며 한 통도 안 나갑니다.
    설정 누락은 "모름"이 아니라 요청이 나가지도 않은 것이 확실한 경우입니다.

    Returns:
        tuple[str, str]: (계정, 인증키)

    Raises:
        PpurioError: 환경변수가 없을 때
    """
    try:
        return os.environ["PPURIO_ACCOUNT"], os.environ["PPURIO_API_KEY"]
    except KeyError as error:
        raise PpurioError(0, f"환경변수 {error.args[0]} 가 없습니다") from error


def _sender() -> str:
    """발신번호를 환경변수에서 읽습니다.

    계정에 사전등록된 번호입니다(발신번호 사전등록제). 계정·토큰과 같은 성격이라
    도메인 계층이 아니라 여기서 채웁니다.

    Returns:
        str: 발신번호

    Raises:
        PpurioError: 환경변수가 없을 때
    """
    try:
        return os.environ["PPURIO_SENDER"]
    except KeyError as error:
        raise PpurioError(0, f"환경변수 {error.args[0]} 가 없습니다") from error


def _post(path: str, body: dict, headers: dict) -> dict:
    """뿌리오에 POST 합니다.

    Args:
        path: /v1/message 같은 경로
        body: 요청 본문
        headers: Authorization 을 포함한 헤더

    Returns:
        dict: 응답 본문

    Raises:
        PpurioError: 2xx 가 아닐 때
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

    실패는 무엇이든 PpurioError 로 바꿉니다. 예외 종류를 열거하지 않는 이유는,
    "토큰 단계에서 터졌으면 /v1/message 는 만들어지지도 않았다"가 종류와 무관하게
    참이기 때문입니다. 프록시가 200 에 HTML 을 실어 주면 JSONDecodeError,
    EUC-KR 로 주면 UnicodeDecodeError 가 나는데, 열거로 잡으면 그것들이 새어
    나가 호출부의 except PpurioError 를 비켜갑니다. 그러면 이력 시트에 자리는
    잡힌 채 접수코드가 빈 행으로 남고, 빈 칸은 "보냈는지 모름"이라 살아 있는
    것으로 취급되어 그 campaign 이 영구히 잠깁니다.

    조용히 삼키지 않습니다. 사유를 담아 타입만 바꿔 다시 던집니다.

    Returns:
        str: Bearer 토큰

    Raises:
        PpurioError: 발급에 실패했을 때
    """
    account, key = _credentials()
    basic = base64.b64encode(f"{account}:{key}".encode()).decode()
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

    account 만 채워 넣습니다. messageType·content·targets·sendTime 등은
    호출부가 준 값을 그대로 보냅니다.

    Args:
        payload: 뿌리오 /v1/message 요청 본문 (account 제외)

    Returns:
        dict: 뿌리오 응답. code 가 "1000" 이면 접수 성공

    Raises:
        PpurioError: HTTP 실패
    """
    account, _ = _credentials()
    # from 은 필수다. 없으면 벤더가 거절하고, 그때는 이미 이력 시트에 자리를
    # 잡은 뒤라 전 행이 '실패'로 찍힌다. payload 가 이기도록 뒤에 펼쳐 둔다.
    body = {"account": account, "from": _sender(), **payload}
    # 토큰 단계의 실패는 issue_token 이 PpurioError 로 바꿔 던진다.
    # 이 호출의 타임아웃은 다르다 — 접수됐을 수 있으므로 그대로 전파해
    # 접수코드를 비운 채 사람이 확인하게 둔다.
    return _post("/v1/message", body, {"Authorization": "Bearer " + issue_token()})


def cancel(message_key: str) -> dict:
    """예약 발송을 취소합니다. 발송 1분 전까지만 가능합니다.

    Args:
        message_key: 접수 시 받은 messageKey

    Returns:
        dict: 뿌리오 응답
    """
    account, _ = _credentials()
    return _post(
        "/v1/cancel",
        {"account": account, "messageKey": message_key},
        {"Authorization": "Bearer " + issue_token()},
    )
