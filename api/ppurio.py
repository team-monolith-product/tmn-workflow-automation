"""
뿌리오 문자 발송 API 래퍼 함수

인증 정보는 환경 변수로 받는다.
- PPURIO_ACCOUNT: 뿌리오 계정 ID
- PPURIO_API_KEY: 연동 인증키 (없으면 PPURIO 로 폴백)
- PPURIO_SENDER: 사전 등록된 발신번호

뿌리오는 호출 IP를 사전 등록해야 하며(미등록 시 code 3003 invalid ip),
발송 결과 조회 엔드포인트가 없다. 응답이 code 1000이어도 '접수 성공'일 뿐이므로
최종 도달 여부는 service.ppurio_result 로 확인한다.
"""

import base64
import os

import requests

BASE_URL = "https://message.ppurio.com"
REQUEST_TIMEOUT = 20


def get_account() -> str:
    """뿌리오 계정 ID"""
    return os.environ["PPURIO_ACCOUNT"]


def get_sender() -> str:
    """뿌리오에 등록된 발신번호"""
    return os.environ["PPURIO_SENDER"]


def post_token() -> dict:
    """액세스 토큰 발급 (Basic 인증)

    Returns:
        dict: {"token": "...", "expired": "..."} 또는 실패 시 code/description
    """
    api_key = os.environ.get("PPURIO_API_KEY") or os.environ["PPURIO"]
    basic = base64.b64encode(f"{get_account()}:{api_key}".encode()).decode()
    response = requests.post(
        f"{BASE_URL}/v1/token",
        headers={"Authorization": f"Basic {basic}"},
        timeout=REQUEST_TIMEOUT,
    )
    return response.json()


def post_message(token: str, payload: dict) -> dict:
    """문자 발송 요청

    Args:
        token: post_token 으로 발급받은 액세스 토큰
        payload: 뿌리오 발송 페이로드 (account, messageType, from, content, targets 등)

    Returns:
        dict: {"code": "1000", "description": "정상", "messageKey": "..."} 형태.
            실패도 HTTP 200에 code/description 으로 오므로 그대로 반환한다.
    """
    response = requests.post(
        f"{BASE_URL}/v1/message",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json;charset=utf-8",
        },
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    return response.json()


def post_cancel(token: str, message_key: str) -> dict:
    """예약 발송 취소 (발송 1분 전까지만 가능)

    Args:
        token: 액세스 토큰
        message_key: 발송 응답의 messageKey

    Returns:
        dict: 취소 결과 (code, description)
    """
    response = requests.post(
        f"{BASE_URL}/v1/cancel",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json;charset=utf-8",
        },
        json={"account": get_account(), "messageKey": message_key},
        timeout=REQUEST_TIMEOUT,
    )
    return response.json()
