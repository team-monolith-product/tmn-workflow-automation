"""
AWS Athena 관련 LangChain Tools
"""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from api import athena
from app.tools.slack_place import SlackPlace


class AthenaQueryInput(BaseModel):
    """Athena 쿼리 입력"""

    query: str = Field(description="실행할 SQL 쿼리")
    database: str = Field(description="사용할 Athena 데이터베이스 이름 (필수)")


# 슬랙 스레드가 있을 때만 붙인다. 스레드가 없으면 show_result_to_user 가 없으므로,
# 이 지침이 있으면 에이전트가 결과를 보여줄 길이 없다고 믿는다.
SLACK_RESULT_GUIDE = """

**show_result_to_user**: 사용자에게 쿼리 결과를 보여주고 싶으면 반드시 이 값을 true로 설정해야 합니다.
이 값이 true일 때만 사용자가 결과를 볼 수 있습니다.
agent가 직접 표를 작성하여 답변하는 것은 허용되지 않습니다."""


def format_query_results_as_markdown(results: dict) -> str:
    """
    Athena 쿼리 결과를 마크다운 테이블 형식으로 포맷팅합니다.

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


def format_query_results_as_slack_table(results: dict) -> dict:
    """
    Athena 쿼리 결과를 Slack Block Kit table 형식으로 포맷팅합니다.

    Slack table block 공식 문서:
    https://docs.slack.dev/reference/block-kit/blocks/table-block/

    Args:
        results: Athena 쿼리 결과 (원본 응답)

    Returns:
        dict: Slack table block
    """
    if "ResultSet" not in results:
        return {
            "type": "section",
            "text": {"type": "plain_text", "text": "결과가 없습니다."},
        }

    result_set = results["ResultSet"]
    rows = result_set.get("Rows", [])

    if not rows:
        return {
            "type": "section",
            "text": {"type": "plain_text", "text": "결과가 없습니다."},
        }

    # Slack table block 형식으로 변환
    # 첫 번째 행은 헤더, 나머지는 데이터 행
    # 각 셀은 {"type": "raw_text", "text": "내용"} 형식
    table_rows = []
    for row in rows:
        cells = [
            {"type": "raw_text", "text": col.get("VarCharValue", "")}
            for col in row["Data"]
        ]
        table_rows.append(cells)

    table_block = {
        "type": "table",
        "rows": table_rows,
    }

    return table_block


ATHENA_QUERY_DESCRIPTION = """AWS Athena에서 SQL 쿼리를 실행하고 결과를 반환합니다.

이 도구는 데이터베이스에서 데이터를 조회할 때 사용합니다.
쿼리는 표준 SQL 문법을 따릅니다.

**중요**: database 파라미터는 필수입니다.
Redash 쿼리를 참고할 때는 해당 쿼리에서 사용된 데이터베이스를 확인하고
동일한 데이터베이스를 반드시 지정해야 합니다.

**SHOW TABLES 구문 주의사항**:
Athena의 SHOW TABLES는 SQL 표준 LIKE가 아닌 정규표현식을 사용합니다.
- ❌ 잘못된 예: SHOW TABLES IN database_name LIKE '%pattern%'
- ✅ 올바른 예: SHOW TABLES IN database_name '*pattern*'
- LIKE 키워드를 사용하지 마세요
- % 대신 * 또는 .* 을 사용하세요
- 예: SHOW TABLES IN jce_prd '*activ*' (activ를 포함하는 모든 테이블)
- 예: SHOW TABLES IN jce_prd 'class_*' (class_로 시작하는 모든 테이블)

결과는 마크다운 표로 돌려줍니다."""

# Slack section block text 길이 제한 (3000자보다 여유있게)
MAX_QUERY_LENGTH = 2900


class SlackAthenaQueryInput(AthenaQueryInput):
    """슬랙 스레드가 있을 때의 입력. 결과를 스레드에 올릴 수 있다."""

    show_result_to_user: bool = Field(
        default=False,
        description="결과를 사용자에게 Slack 메시지로 직접 전송할지 여부",
    )


async def _post_result(place: SlackPlace, query: str, results: dict) -> str:
    """쿼리와 결과 표를 스레드에 올립니다.

    Args:
        place: 결과를 올릴 슬랙 스레드
        query: 실행한 SQL
        results: Athena 결과

    Returns:
        str: 에이전트에게 돌려줄 안내
    """
    # 쿼리가 너무 길면 코드 스니펫으로 업로드, 짧으면 코드 블록으로 표시
    if len(query) > MAX_QUERY_LENGTH:
        await place.client.files_upload_v2(
            channel=place.channel,
            content=query,
            filename="query.sql",
            title="실행된 SQL 쿼리",
            thread_ts=place.thread_ts,
        )
    else:
        await place.say(
            {
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"```\n{query}\n```"},
                    }
                ]
            },
            thread_ts=place.thread_ts,
        )

    await place.say(
        {"blocks": [format_query_results_as_slack_table(results)]},
        thread_ts=place.thread_ts,
    )

    # 행 수 계산 (헤더 제외)
    rows = results.get("ResultSet", {}).get("Rows", [])
    row_count = len(rows) - 1 if len(rows) > 0 else 0
    return f"쿼리 결과 총 {row_count}행을 슬랙 메시지로 전송했습니다."


def get_execute_athena_query_tool(place: SlackPlace | None = None) -> StructuredTool:
    """
    Athena 쿼리 실행 도구를 반환합니다.

    스레드가 있으면 결과를 스레드에 올리는 show_result_to_user 를 받고, 없으면
    그 인자가 없습니다. 보낼 곳이 없는데 인자가 보이면 에이전트가 켜고는 결과가
    갔다고 믿습니다.

    Args:
        place: 결과를 올릴 슬랙 스레드. 없으면 결과는 반환값으로만 돌아갑니다

    Returns:
        execute_athena_query tool
    """

    async def run(query: str, database: str, post_to: SlackPlace | None) -> str:
        try:
            results = await athena.execute_and_wait(query, database=database)
            if post_to:
                return await _post_result(post_to, query, results)
            # Agent가 분석용으로 사용할 때는 마크다운 테이블 반환
            return format_query_results_as_markdown(results)
        except Exception as e:
            return f"쿼리 실행 중 오류 발생: {str(e)}"

    if place is None:

        async def execute_athena_query(query: str, database: str) -> str:
            return await run(query, database, None)

        return StructuredTool.from_function(
            coroutine=execute_athena_query,
            name="execute_athena_query",
            description=ATHENA_QUERY_DESCRIPTION,
            args_schema=AthenaQueryInput,
        )

    async def execute_athena_query_in_thread(
        query: str, database: str, show_result_to_user: bool = False
    ) -> str:
        return await run(query, database, place if show_result_to_user else None)

    return StructuredTool.from_function(
        coroutine=execute_athena_query_in_thread,
        name="execute_athena_query",
        description=ATHENA_QUERY_DESCRIPTION + SLACK_RESULT_GUIDE,
        args_schema=SlackAthenaQueryInput,
    )


# 기본 tool (backward compatibility를 위해 유지)
execute_athena_query = get_execute_athena_query_tool()
