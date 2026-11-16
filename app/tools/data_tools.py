"""
데이터 분석 도구 묶음입니다.

팀모노리스 로봇(app/general.py)과 MCP(app/robot_mcp.py)가 모두 여기서 받습니다.
둘이 같은 도구를 쓰는 것을 조립하는 곳을 하나로 두어 보장합니다 -- 따로
조립하면 한쪽에만 도구가 생기거나 빠집니다. 도구의 사용
지침(DATA_GUIDE·SLACK_DATA_GUIDE)도 도구 곁인 여기에 둡니다.
"""

from collections.abc import Awaitable, Callable

from langchain_core.tools import BaseTool

from app.tools.athena_tools import get_execute_athena_query_tool
from app.tools.python_tools import get_execute_python_tool
from app.tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)
from app.tools.slack_place import SlackPlace

# 데이터 분석 지침.
#
# 어디서 물어도 같은 DB·도구 안내(DATA_GUIDE)와, 결과가 슬랙 스레드로 가는 경우의
# 지침(SLACK_DATA_GUIDE)을 나눠 둔다. MCP 는 스레드가 없어 앞의 것만 쓴다
# (app/robot_mcp.py). 슬랙 지침을 MCP 에 주면 에이전트가 결과를 표로 보여주지 못한다.
DATA_GUIDE: str = (
    "\n\n"
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
    "**파이썬 실행**:\n"
    "- 집계·대조·명단 추출은 execute_python 도구를 사용하세요.\n"
    "- 코드 내에서 `execute_athena_query(query, database)` 함수를 직접 호출할 수 있습니다.\n"
    "- 쿼리 작성 시 Athena(Presto) SQL 문법을 사용합니다.\n"
)

SLACK_DATA_GUIDE: str = (
    "\n"
    "**중요**: 사용자에게 쿼리 결과를 보여주고 싶을 때:\n"
    "- execute_athena_query 도구를 호출할 때 반드시 show_result_to_user=True를 설정해야 합니다.\n"
    "- 이 값이 true일 때만 사용자가 Slack에서 결과를 볼 수 있습니다.\n"
    "- 절대로 직접 표를 작성하여 답변하지 마세요. 반드시 show_result_to_user=True를 사용하세요.\n"
    "\n"
    "**파이썬 실행 결과 전달**:\n"
    "- 시각화도 execute_python 도구로 합니다. 차트를 그리면 슬랙에 자동으로 업로드합니다.\n"
    "- plt.savefig()나 plt.show()를 호출하지 마세요. 자동으로 처리됩니다.\n"
    "- CSV·엑셀·ZIP 등 결과 파일은 코드 안에서 `upload_file(filename, data)`로 스레드에 직접 첨부하세요. 드라이브·S3 링크를 요청하지 마세요.\n"
)


def get_redash_tools() -> list[BaseTool]:
    """Redash 대시보드·쿼리 조회 도구입니다. 어디서 불러도 똑같이 돕니다.

    Returns:
        list[BaseTool]: [대시보드 찾기, 대시보드 읽기, 쿼리 읽기]
    """
    return [list_redash_dashboards, read_redash_dashboard, read_redash_query]


def get_execution_tools(
    place: SlackPlace | None,
    *,
    draft_sms: Callable[..., Awaitable[str]] | None = None,
) -> list[BaseTool]:
    """Athena·파이썬 실행 도구입니다. place 에 따라 결과를 올리는 방법이 달라집니다.

    Args:
        place: 결과를 올릴 슬랙 스레드. 없으면 결과는 도구 반환값으로만 돌아갑니다
        draft_sms: 파이썬 코드 안에서 부를 문자 초안 코루틴

    Returns:
        list[BaseTool]: [Athena 실행, 파이썬 실행]
    """
    return [
        get_execute_athena_query_tool(place),
        get_execute_python_tool(place, draft_sms=draft_sms),
    ]


def get_data_tools(place: SlackPlace | None) -> list[BaseTool]:
    """Redash·Athena·파이썬 도구를 돌려줍니다.

    Args:
        place: 결과를 올릴 슬랙 스레드. 없으면 결과는 도구 반환값으로만 돌아갑니다

    Returns:
        list[BaseTool]: 데이터 분석 도구
    """
    return get_redash_tools() + get_execution_tools(place)
