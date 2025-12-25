"""
AWS Athena 관련 LangChain Tools
"""

from typing import Annotated
from langchain_core.tools import tool
from api import athena


def format_query_results(results: dict) -> str:
    """
    Athena 쿼리 결과를 읽기 쉬운 형태로 포맷팅합니다.

    Args:
        results: Athena 쿼리 결과 (원본 응답)

    Returns:
        str: 포맷된 결과 문자열
    """
    if "ResultSet" not in results:
        return "결과가 없습니다."

    result_set = results["ResultSet"]
    rows = result_set.get("Rows", [])

    if not rows:
        return "결과가 없습니다."

    # 첫 번째 행은 컬럼 헤더
    headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]

    # 나머지 행은 데이터
    data_rows = []
    for row in rows[1:]:
        data = [col.get("VarCharValue", "") for col in row["Data"]]
        data_rows.append(data)

    # 마크다운 테이블 형식으로 포맷
    if not headers:
        return "결과가 없습니다."

    # 헤더 행
    formatted = "| " + " | ".join(headers) + " |\n"
    # 구분선
    formatted += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    # 데이터 행
    for row in data_rows:
        formatted += "| " + " | ".join(row) + " |\n"

    return formatted


@tool
def execute_athena_query(
    query: Annotated[str, "실행할 SQL 쿼리"],
    database: Annotated[str, "사용할 Athena 데이터베이스 이름 (필수)"],
) -> str:
    """
    AWS Athena에서 SQL 쿼리를 실행하고 결과를 반환합니다.

    이 도구는 데이터베이스에서 데이터를 조회할 때 사용합니다.
    쿼리는 표준 SQL 문법을 따릅니다.

    **중요**: database 파라미터는 필수입니다.
    Redash 쿼리를 참고할 때는 해당 쿼리에서 사용된 데이터베이스를 확인하고
    동일한 데이터베이스를 반드시 지정해야 합니다.

    Args:
        query: 실행할 SQL 쿼리
        database: 사용할 Athena 데이터베이스 이름 (필수)

    Returns:
        str: 쿼리 실행 결과 (마크다운 테이블 형식)
    """
    try:
        results = athena.execute_and_wait(query, database=database)
        return format_query_results(results)
    except Exception as e:
        return f"쿼리 실행 중 오류 발생: {str(e)}"
