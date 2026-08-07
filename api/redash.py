"""
Redash API 래퍼 함수들
"""

import os
import aiohttp


def get_base_url() -> str:
    """
    Redash 기본 URL을 환경 변수에서 가져옵니다.

    Returns:
        str: Redash 기본 URL
    """
    return os.environ.get("REDASH_BASE_URL", "")


def get_api_key() -> str:
    """
    Redash API 키를 환경 변수에서 가져옵니다.

    Returns:
        str: Redash API 키
    """
    return os.environ.get("REDASH_API_KEY", "")


def get_headers() -> dict[str, str]:
    """
    Redash API 요청 헤더를 생성합니다.

    Returns:
        dict: API 요청 헤더
    """
    return {"Authorization": f"Key {get_api_key()}", "Content-Type": "application/json"}


async def _get_json(url: str, params: dict[str, str] | None = None) -> dict:
    """
    Redash API에 GET 요청을 보내고 JSON 응답을 반환합니다.

    Args:
        url: 요청할 전체 URL
        params: 쿼리 파라미터 (aiohttp는 문자열 값만 받는다)

    Returns:
        dict: 응답 JSON
    """
    async with aiohttp.ClientSession(headers=get_headers()) as session:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            return await response.json()


async def list_dashboards(query: str | None = None) -> dict:
    """
    대시보드 목록을 조회합니다.

    Args:
        query: 검색어 (선택사항)

    Returns:
        dict: 대시보드 목록 (원본 Redash 응답)
    """
    url = f"{get_base_url()}/api/dashboards"
    params = {"q": query} if query else None

    return await _get_json(url, params)


async def get_dashboard(dashboard_slug: str) -> dict:
    """
    특정 대시보드의 상세 정보를 조회합니다.

    Args:
        dashboard_slug: 대시보드 슬러그 (URL에 사용되는 식별자)

    Returns:
        dict: 대시보드 상세 정보 (원본 Redash 응답)
    """
    url = f"{get_base_url()}/api/dashboards/{dashboard_slug}"

    return await _get_json(url)


async def get_query(query_id: int) -> dict:
    """
    특정 쿼리의 상세 정보를 조회합니다.

    Args:
        query_id: 쿼리 ID

    Returns:
        dict: 쿼리 상세 정보 (원본 Redash 응답)
    """
    url = f"{get_base_url()}/api/queries/{query_id}"

    return await _get_json(url)


async def search_queries(query: str, page: int = 1, page_size: int = 25) -> dict:
    """
    쿼리를 검색합니다.

    Args:
        query: 검색어
        page: 페이지 번호
        page_size: 페이지 크기

    Returns:
        dict: 검색 결과 (원본 Redash 응답)
    """
    url = f"{get_base_url()}/api/queries"
    params = {"q": query, "page": str(page), "page_size": str(page_size)}

    return await _get_json(url, params)
