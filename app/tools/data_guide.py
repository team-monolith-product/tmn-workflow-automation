"""
Athena·Redash 로 데이터를 조회할 때의 안내입니다.

어디서 물어도 같은 내용이라 슬랙 로봇(app/general.py)과 MCP(app/robot_mcp.py)가
함께 씁니다. 결과를 슬랙 스레드에 올리는 법처럼 로봇에만 맞는 안내는 로봇의
ROBOT_INSTRUCTIONS 에 따로 있습니다.
"""

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
)
