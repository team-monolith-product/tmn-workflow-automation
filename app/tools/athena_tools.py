"""
AWS Athena 관련 LangChain Tools
"""

from typing import Annotated, Callable, Any
from langchain_core.tools import tool
from api import athena

# 데이터 분석에 필요한 Athena DB 안내와 도구 사용 지침.
# 데이터 봇과 대표 봇(로봇)이 같은 지침으로 분석하도록 함께 쓴다.
DATA_ANALYSIS_GUIDE = (
    "Athena 데이터베이스 가이드:\n"
    "- 네이밍: {서비스}_{env}_{region} (env: dev/prd, region: 서울은 생략, 중동=me, 영국=uk)\n"
    "- jce_{env}_{region}: 코들 서비스 원본 (교실/교재/활동/학생/교사/제출물/구독). class_ 접두사=수업, user_ 접두사=사용자\n"
    "- datadb_{env}_{region}: AI Chatkit 대화 세션/스레드/메시지\n"
    "- ga_{env}: 코들 GA 웹사이트 행동 이벤트, 유입 경로(UTM), 디바이스/지역\n"
    "- kafka_{env}: 서비스 내부 실시간 이벤트 (LLM 호출 로그, 동영상 재생, 교재 수정 등)\n"
    "- data_warehouse_{env}: 1차 정제 분석 데이터 (활성 교실/교재/교사, 사용자 활성화, GA 행동, 짓다 GA 행동(jitda_ga_* 테이블), 연수 로그, AI 퀴즈 등)\n"
    "- data_mart_{env}: 2차 정제 핵심 지표 (DAU, 마케팅 동의, 에러 커버리지, 세션 전환율 등)\n"
    "- monday_{env}: 영업/계약 데이터 (학교별 계약 상태/금액/기간/담당자)\n"
    "- default: 인프라 로그 (ALB/CloudFront 액세스 로그)\n"
    "- jitda_opencode_{env}: 짓다 OpenCode 파이프라인 데이터\n"
    "- enk_hackathon_{env}: 짓다(enk) 해커톤 파이프라인 데이터\n"
    "- jitda_ga_{env}: 짓다(jitda.io) GA 웹사이트 행동 이벤트, 유입 경로(UTM), 디바이스/지역 (raw 테이블 jitda_google_analytics, 파싱 뷰 parsed_ga)\n"
    "- jitda_gsc_{env}: 짓다(jitda.io) Google Search Console 검색 성과. searchdata_site_impression(사이트 단위 검색어·국가·기기별 노출/클릭/순위), searchdata_url_impression(페이지 URL·검색어별 노출/클릭/순위, is_* 검색 노출 유형)\n"
    "\n"
    "파티션 필터링 (DB마다 다름):\n"
    "- jce/datadb: WHERE partition_0 = 'YYYY-MM-DD'\n"
    "- ga/jitda_ga: WHERE event_date = 'YYYYMMDD'\n"
    "- jitda_gsc: WHERE data_date = 'YYYY-MM-DD' (미국 태평양 시간 기준 날짜, 약 2일 늦게 들어옴)\n"
    "- data_warehouse/data_mart/kafka: WHERE date = 'YYYY-MM-DD'\n"
    "\n"
    "주의사항:\n"
    "- ga_prd.parsed_ga는 파티션 간 스키마 불일치 → raw 테이블(google_analytics) 사용 권장\n"
    "- ga_prd.google_analytics의 is_active_user도 타입 불일치 → SELECT * 대신 컬럼 명시\n"
    "- GA(ga/jitda_ga)의 traffic_source, collected_traffic_source는 JSON → json_extract_scalar()로 파싱\n"
    "- jitda_gsc의 sum_top_position·sum_position은 0부터 센 순위 합 → 평균 순위 = 합 / impressions + 1\n"
    "\n"
    "Instructions:\n"
    "- 데이터 분석 요청에 대해 위 데이터베이스 가이드를 참고하여 적절한 DB를 선택합니다.\n"
    "- 필요시 Redash 대시보드를 검색하여 관련 쿼리 패턴을 참고합니다.\n"
    "- Redash에서 찾은 쿼리를 참고하거나, 직접 Athena에서 SQL 쿼리를 작성하여 실행합니다.\n"
    "\n"
    "**중요**: 사용자에게 쿼리 결과를 보여주고 싶을 때:\n"
    "- execute_athena_query 도구를 호출할 때 반드시 show_result_to_user=True를 설정해야 합니다.\n"
    "- 이 값이 true일 때만 사용자가 Slack에서 결과를 볼 수 있습니다.\n"
    "- 절대로 직접 표를 작성하여 답변하지 마세요. 반드시 show_result_to_user=True를 사용하세요.\n"
    "\n"
    "**파이썬 실행**:\n"
    "- 집계·대조·명단 추출·시각화는 execute_python 도구를 사용하세요.\n"
    "- 파이썬 코드를 실행하고, 차트를 그리면 슬랙에 자동으로 업로드합니다.\n"
    "- 코드 내에서 `execute_athena_query(query, database)` 함수를 직접 호출할 수 있습니다.\n"
    "- plt.savefig()나 plt.show()를 호출하지 마세요. 자동으로 처리됩니다.\n"
    "- CSV·엑셀·ZIP 등 결과 파일은 코드 안에서 `upload_file(filename, data)`로 스레드에 직접 첨부하세요. 드라이브·S3 링크를 요청하지 마세요.\n"
)


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


def get_execute_athena_query_tool(
    say: Callable[[dict[str, Any], str], Any] | None = None,
    thread_ts: str | None = None,
    slack_client: Any | None = None,
    channel: str | None = None,
):
    """
    Athena 쿼리 실행 도구를 반환합니다.

    Args:
        say: Slack 메시지 전송 함수
        thread_ts: Slack 스레드 타임스탬프
        slack_client: Slack 클라이언트 (파일 업로드용)
        channel: Slack 채널 ID

    Returns:
        execute_athena_query tool
    """

    @tool
    async def execute_athena_query(
        query: Annotated[str, "실행할 SQL 쿼리"],
        database: Annotated[str, "사용할 Athena 데이터베이스 이름 (필수)"],
        show_result_to_user: Annotated[
            bool, "결과를 사용자에게 Slack 메시지로 직접 전송할지 여부"
        ] = False,
    ) -> str:
        """
        AWS Athena에서 SQL 쿼리를 실행하고 결과를 반환합니다.

        이 도구는 데이터베이스에서 데이터를 조회할 때 사용합니다.
        쿼리는 표준 SQL 문법을 따릅니다.

        **중요**: database 파라미터는 필수입니다.
        Redash 쿼리를 참고할 때는 해당 쿼리에서 사용된 데이터베이스를 확인하고
        동일한 데이터베이스를 반드시 지정해야 합니다.

        **show_result_to_user**: 사용자에게 쿼리 결과를 보여주고 싶으면 반드시 이 값을 true로 설정해야 합니다.
        이 값이 true일 때만 사용자가 결과를 볼 수 있습니다.
        agent가 직접 표를 작성하여 답변하는 것은 허용되지 않습니다.

        **SHOW TABLES 구문 주의사항**:
        Athena의 SHOW TABLES는 SQL 표준 LIKE가 아닌 정규표현식을 사용합니다.
        - ❌ 잘못된 예: SHOW TABLES IN database_name LIKE '%pattern%'
        - ✅ 올바른 예: SHOW TABLES IN database_name '*pattern*'
        - LIKE 키워드를 사용하지 마세요
        - % 대신 * 또는 .* 을 사용하세요
        - 예: SHOW TABLES IN jce_prd '*activ*' (activ를 포함하는 모든 테이블)
        - 예: SHOW TABLES IN jce_prd 'class_*' (class_로 시작하는 모든 테이블)

        Args:
            query: 실행할 SQL 쿼리
            database: 사용할 Athena 데이터베이스 이름 (필수)
            show_result_to_user: 결과를 사용자에게 Slack으로 전송할지 여부 (기본값: False)

        Returns:
            str: show_result_to_user가 False이면 쿼리 실행 결과 (마크다운 테이블 형식),
                 True이면 "쿼리 결과 총 {행수}행을 슬랙 메시지로 전송했습니다."
        """
        MAX_QUERY_LENGTH = (
            2900  # Slack section block text 길이 제한 (3000자보다 여유있게)
        )

        try:
            results = await athena.execute_and_wait(query, database=database)

            if show_result_to_user and say and thread_ts:
                # 1. 먼저 사용된 SQL 쿼리를 전송
                # 쿼리가 너무 길면 코드 스니펫으로 업로드, 짧으면 코드 블록으로 표시
                if len(query) > MAX_QUERY_LENGTH and slack_client and channel:
                    # 코드 스니펫으로 업로드
                    await slack_client.files_upload_v2(
                        channel=channel,
                        content=query,
                        filename="query.sql",
                        title="실행된 SQL 쿼리",
                        thread_ts=thread_ts,
                    )
                else:
                    # 코드 블록으로 전송
                    await say(
                        {
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": f"```\n{query}\n```",
                                    },
                                }
                            ]
                        },
                        thread_ts=thread_ts,
                    )

                # 2. 그 다음 쿼리 결과를 table block으로 전송
                table_block = format_query_results_as_slack_table(results)
                await say(
                    {"blocks": [table_block]},
                    thread_ts=thread_ts,
                )

                # 행 수 계산 (헤더 제외)
                result_set = results.get("ResultSet", {})
                rows = result_set.get("Rows", [])
                row_count = len(rows) - 1 if len(rows) > 0 else 0

                return f"쿼리 결과 총 {row_count}행을 슬랙 메시지로 전송했습니다."

            # Agent가 분석용으로 사용할 때는 마크다운 테이블 반환
            return format_query_results_as_markdown(results)
        except Exception as e:
            return f"쿼리 실행 중 오류 발생: {str(e)}"

    return execute_athena_query


# 기본 tool (backward compatibility를 위해 유지)
execute_athena_query = get_execute_athena_query_tool()
