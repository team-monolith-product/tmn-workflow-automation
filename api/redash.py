"""
Redash API 래퍼 함수들
"""

import os
import requests


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


def _strip_query_data(data: dict) -> None:
    """
    Redash API 응답에서 latest_query_data를 제거합니다.
    이 필드는 캐시된 전체 쿼리 결과셋을 포함하며, 대용량일 경우 OOM을 유발할 수 있습니다.
    대시보드 응답의 경우 위젯 내 중첩된 쿼리에서도 제거합니다.
    """
    data.pop("latest_query_data", None)
    for widget in data.get("widgets", []):
        viz = widget.get("visualization")
        if viz and isinstance(viz, dict):
            query = viz.get("query")
            if query and isinstance(query, dict):
                query.pop("latest_query_data", None)


def list_dashboards(query: str | None = None) -> dict:
    """
    대시보드 목록을 조회합니다.

    Args:
        query: 검색어 (선택사항)

    Returns:
        dict: 대시보드 목록 (원본 Redash 응답)
    """
    url = f"{get_base_url()}/api/dashboards"
    params = {}
    if query:
        params["q"] = query

    response = requests.get(url, headers=get_headers(), params=params)
    response.raise_for_status()
    return response.json()


def get_dashboard(dashboard_slug: str) -> dict:
    """
    특정 대시보드의 상세 정보를 조회합니다.

    Args:
        dashboard_slug: 대시보드 슬러그 (URL에 사용되는 식별자)

    Returns:
        dict: 대시보드 상세 정보 (latest_query_data 제외)
    """
    url = f"{get_base_url()}/api/dashboards/{dashboard_slug}"
    response = requests.get(url, headers=get_headers())
    response.raise_for_status()
    data = response.json()
    _strip_query_data(data)
    return data


def get_query(query_id: int) -> dict:
    """
    특정 쿼리의 상세 정보를 조회합니다.

    Args:
        query_id: 쿼리 ID

    Returns:
        dict: 쿼리 상세 정보 (latest_query_data 제외)
    """
    url = f"{get_base_url()}/api/queries/{query_id}"
    response = requests.get(url, headers=get_headers())
    response.raise_for_status()
    data = response.json()
    _strip_query_data(data)
    return data


def search_queries(query: str, page: int = 1, page_size: int = 25) -> dict:
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
    params = {"q": query, "page": page, "page_size": page_size}
    response = requests.get(url, headers=get_headers(), params=params)
    response.raise_for_status()
    return response.json()
