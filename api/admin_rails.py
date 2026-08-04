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
