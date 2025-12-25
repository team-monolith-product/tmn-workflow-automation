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
            slug = dashboard.get("slug", "")
            tags = dashboard.get("tags", [])
            tags_str = ", ".join(tags) if tags else "태그 없음"

            result.append(f"- **{name}** (slug: `{slug}`, 태그: {tags_str})")

        return "\n".join(result)
    except Exception as e:
        return f"대시보드 목록 조회 중 오류 발생: {str(e)}"


@tool
def read_redash_dashboard(
    slug: Annotated[str, "대시보드 슬러그"],
) -> str:
    """
    Redash 대시보드의 상세 정보를 조회합니다.

    이 도구는 특정 대시보드에 포함된 쿼리들을 확인할 때 사용합니다.
    대시보드의 각 위젯에 연결된 쿼리 정보(쿼리 내용, 데이터베이스, 테이블 스키마, JOIN 패턴 등)를 반환합니다.

    Args:
        slug: 대시보드 슬러그 (URL에 사용되는 식별자)

    Returns:
        str: 대시보드 상세 정보 (쿼리 내용 및 데이터베이스 정보 포함)
    """
    try:
        dashboard_data = redash.get_dashboard(slug)

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
            query_id = query_data.get("id")
            query_string = query_data.get("query", "")

            # 쿼리 상세 정보 조회하여 data source 정보 가져오기
            try:
                query_detail = redash.get_query(query_id)
                data_source_id = query_detail.get("data_source_id")

                # data source 이름 추출 (options에서)
                options = query_detail.get("options", {})
                # 일반적으로 Redash 쿼리 응답에 data source 이름이 포함됨
                data_source_name = options.get("data_source", "알 수 없음")
            except Exception:
                data_source_id = None
                data_source_name = "알 수 없음"

            result.append(f"\n## 쿼리: {query_name} (ID: {query_id})")

            if data_source_id:
                result.append(f"**Data Source ID**: {data_source_id}")
            if data_source_name != "알 수 없음":
                result.append(f"**Data Source**: {data_source_name}")

            result.append(f"\n```sql\n{query_string}\n```\n")

            # 쿼리에서 사용된 데이터베이스 추출 시도
            if "FROM" in query_string.upper():
                # database.table 패턴 찾기
                import re

                db_table_pattern = (
                    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b"
                )
                matches = re.findall(db_table_pattern, query_string)
                if matches:
                    databases = set([match[0] for match in matches])
                    result.append(f"**사용된 데이터베이스**: {', '.join(databases)}")

                result.append(
                    "\n_이 쿼리를 참고하여 유사한 분석을 수행할 수 있습니다._"
                )
                result.append("_Athena 쿼리 실행 시 동일한 데이터베이스를 지정하세요._")

        return "\n".join(result)
    except Exception as e:
        return f"대시보드 조회 중 오류 발생: {str(e)}"
