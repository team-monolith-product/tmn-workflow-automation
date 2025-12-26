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
        str: 대시보드 목록 (ID와 이름)
    """
    try:
        response = redash.list_dashboards()
        dashboards = response.get("results", [])

        if not dashboards:
            return "검색 결과가 없습니다."

        result = []
        for dashboard in dashboards:
            dashboard_id = dashboard.get("id")
            name = dashboard.get("name", "Untitled")
            result.append(f"- ID {dashboard_id}: {name}")

        return "\n".join(result)
    except Exception as e:
        return f"대시보드 목록 조회 중 오류 발생: {str(e)}"


@tool
def read_redash_dashboard(
    dashboard_id: Annotated[int, "대시보드 ID"],
) -> str:
    """
    Redash 대시보드의 쿼리 목록을 조회합니다.

    이 도구는 특정 대시보드에 포함된 쿼리 이름과 ID 목록을 확인할 때 사용합니다.
    쿼리의 상세 SQL을 보려면 read_redash_query 도구를 사용하세요.

    Args:
        dashboard_id: 대시보드 ID (list_redash_dashboards에서 조회 가능)

    Returns:
        str: 대시보드의 쿼리 목록 (쿼리 ID와 이름)
    """
    try:
        dashboard_data = redash.get_dashboard(str(dashboard_id))

        name = dashboard_data.get("name", "Untitled")
        widgets = dashboard_data.get("widgets", [])

        result = [f"# 대시보드: {name}\n"]

        if not widgets:
            result.append("이 대시보드에는 위젯이 없습니다.")
            return "\n".join(result)

        # 쿼리 ID와 이름만 수집
        query_list = []
        for widget in widgets:
            visualization = widget.get("visualization")
            if not visualization:
                continue

            query_data = visualization.get("query")
            if not query_data:
                continue

            query_id = query_data.get("id")
            query_name = query_data.get("name", "Untitled Query")

            if query_id:
                query_list.append(f"- Query ID {query_id}: {query_name}")

        if query_list:
            result.append("## 쿼리 목록\n")
            result.extend(query_list)
        else:
            result.append("이 대시보드에는 쿼리가 없습니다.")

        return "\n".join(result)
    except Exception as e:
        return f"대시보드 조회 중 오류 발생: {str(e)}"


@tool
def read_redash_query(
    query_id: Annotated[int, "쿼리 ID"],
) -> str:
    """
    Redash 쿼리의 상세 정보를 조회합니다.

    이 도구는 특정 쿼리의 SQL 내용을 확인할 때 사용합니다.

    Args:
        query_id: 쿼리 ID

    Returns:
        str: 쿼리 상세 정보 (이름, SQL 내용, 데이터베이스 정보)
    """
    try:
        query_data = redash.get_query(query_id)

        query_name = query_data.get("name", "Untitled Query")
        query_string = query_data.get("query", "")

        result = [
            f"# 쿼리: {query_name}",
            f"```sql\n{query_string}\n```",
        ]

        return "\n".join(result)
    except Exception as e:
        return f"쿼리 조회 중 오류 발생: {str(e)}"
