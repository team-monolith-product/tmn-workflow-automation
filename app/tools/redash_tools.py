"""
Redash 관련 LangChain Tools
"""

from typing import Annotated
from langchain_core.tools import tool
from api import redash


@tool
def list_redash_dashboards() -> str:
    """
    Redash 대시보드 목록을 조회합니다.

    이 도구는 모든 대시보드를 조회할 때 사용합니다.

    Returns:
        str: 대시보드 목록 (이름, 슬러그, 태그 포함)
    """
    try:
        response = redash.list_dashboards()
        dashboards = response.get("results", [])

        if not dashboards:
            return "검색 결과가 없습니다."

        result = []
        for dashboard in dashboards:
            name = dashboard.get("name", "Untitled")
            result.append(f"- {name}")

        return "\n".join(result)
    except Exception as e:
        return f"대시보드 목록 조회 중 오류 발생: {str(e)}"


@tool
def read_redash_dashboard(
    slug: Annotated[str, "대시보드 슬러그 또는 ID"],
) -> str:
    """
    Redash 대시보드의 상세 정보를 조회합니다.

    이 도구는 특정 대시보드에 포함된 쿼리들을 확인할 때 사용합니다.
    대시보드의 각 위젯에 연결된 쿼리 정보(쿼리 내용, 데이터베이스, 테이블 스키마, JOIN 패턴 등)를 반환합니다.

    Args:
        slug: 대시보드 슬러그 또는 ID (슬러그인 경우 자동으로 ID로 변환됨)

    Returns:
        str: 대시보드 상세 정보 (쿼리 내용 및 데이터베이스 정보 포함)
    """
    try:
        # slug가 숫자가 아닌 경우, 대시보드 목록에서 ID를 찾음
        dashboard_id = slug
        if not slug.isdigit():
            dashboards_response = redash.list_dashboards()
            dashboards = dashboards_response.get("results", [])

            # slug로 대시보드 찾기
            matching_dashboard = None
            for dashboard in dashboards:
                if dashboard.get("slug") == slug or dashboard.get("name") == slug:
                    matching_dashboard = dashboard
                    break

            if not matching_dashboard:
                return f"'{slug}' 슬러그 또는 이름을 가진 대시보드를 찾을 수 없습니다."

            dashboard_id = str(matching_dashboard.get("id"))

        dashboard_data = redash.get_dashboard(dashboard_id)

        name = dashboard_data.get("name", "Untitled")
        widgets = dashboard_data.get("widgets", [])

        result = [f"# 대시보드: {name}\n"]

        if not widgets:
            result.append("이 대시보드에는 위젯이 없습니다.")
            return "\n".join(result)

        for widget in widgets:
            visualization = widget.get("visualization")
            if not visualization:
                continue

            query_data = visualization.get("query")
            if not query_data:
                continue

            query_name = query_data.get("name", "Untitled Query")
            query_string = query_data.get("query", "")

            result.append(f"\n## 쿼리: {query_name}")
            result.append(f"\n```sql\n{query_string}\n```\n")

        return "\n".join(result)
    except Exception as e:
        return f"대시보드 조회 중 오류 발생: {str(e)}"
