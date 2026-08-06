"""
뿌리오 문자 발송 API 래퍼 함수

인증 정보는 환경 변수로 받는다: PPURIO_ACCOUNT, PPURIO_API_KEY, PPURIO_SENDER.

뿌리오는 호출 IP를 사전 등록해야 하며(미등록 시 code 3003 invalid ip),
발송 결과 조회 엔드포인트가 없다. 응답이 code 1000이어도 '접수 성공'일 뿐이므로
최종 도달 여부는 service.ppurio_result 로 확인한다.
"""

import base64
import os

import requests

BASE_URL = "https://message.ppurio.com"
REQUEST_TIMEOUT = 20


def post_token() -> dict:
    """액세스 토큰 발급 (Basic 인증)

    Returns:
        dict: {"token": "...", "expired": "..."} 또는 실패 시 code/description
    """
    credential = f"{os.environ['PPURIO_ACCOUNT']}:{os.environ['PPURIO_API_KEY']}"
    response = requests.post(
        f"{BASE_URL}/v1/token",
        headers={
            "Authorization": "Basic " + base64.b64encode(credential.encode()).decode()
        },
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
