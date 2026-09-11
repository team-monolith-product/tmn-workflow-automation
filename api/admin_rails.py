"""
admin-rails API 래퍼 함수들
"""

import os
from typing import Any

import aiohttp


def get_base_url() -> str:
    """
    admin-rails 기본 URL을 환경 변수에서 가져옵니다.

    Returns:
        str: admin-rails 기본 URL
    """
    return os.environ["ADMIN_RAILS_BASE_URL"]


async def get_me(access_token: str) -> dict[str, Any] | None:
    """
    액세스 토큰이 가리키는 어드민 정보를 조회합니다.

    토큰이 유효하지 않은 것은 오류가 아니라 검증 결과이므로 401만 None으로
    돌려줍니다. 나머지 실패는 그대로 올립니다.

    Args:
        access_token: admin-rails Doorkeeper가 발급한 액세스 토큰

    Returns:
        dict[str, Any] | None: /api/v1/me 응답. 토큰이 유효하지 않으면 None
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{get_base_url()}/api/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            if response.status == 401:
                return None
            response.raise_for_status()
            return await response.json()


async def _post_token(form: dict[str, str]) -> dict[str, Any] | None:
    """Doorkeeper 토큰 엔드포인트에 폼을 보내고 JSON 을 돌려줍니다.

    코드·리프레시 토큰이 낡은 것(invalid_grant, 400)과 클라이언트가 틀린 것(401)은
    오류가 아니라 「다시 로그인」 판정이라 None 으로 돌려줍니다. 나머지는 그대로 올립니다.
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{get_base_url()}/oauth/token", data=form) as response:
            if response.status in (400, 401):
                return None
            response.raise_for_status()
            return await response.json()


async def exchange_authorization_code(
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    client_secret: str | None = None,
) -> dict[str, Any] | None:
    """
    인가 코드를 액세스 토큰으로 바꿉니다 (authorization_code + PKCE).

    Args:
        code: /oauth/authorize 가 돌려준 인가 코드
        redirect_uri: 인가 요청에 썼던 것과 같은 리다이렉트 주소
        client_id: Doorkeeper 애플리케이션 uid
        code_verifier: 인가 요청의 code_challenge 를 만든 원문
        client_secret: 기밀 클라이언트면 넣는다. 공개 클라이언트는 None

    Returns:
        dict[str, Any] | None: access_token·refresh_token·expires_in 등 원본 응답.
            코드가 낡았으면 None
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    return await _post_token(form)


async def refresh_access_token(
    refresh_token: str, client_id: str, client_secret: str | None = None
) -> dict[str, Any] | None:
    """
    리프레시 토큰으로 새 액세스 토큰을 받습니다.

    Args:
        refresh_token: 이전 토큰 응답의 refresh_token
        client_id: Doorkeeper 애플리케이션 uid
        client_secret: 기밀 클라이언트면 넣는다

    Returns:
        dict[str, Any] | None: 새 토큰 응답 원본. 리프레시 토큰이 폐기됐으면 None
    """
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    return await _post_token(form)
