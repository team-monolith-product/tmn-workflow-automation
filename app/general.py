"""
범용 봇 이벤트 핸들러들
"""

import asyncio
from datetime import datetime, timedelta
import importlib
import traceback

import psycopg
from cachetools import TTLCache
from slack_bolt.context.respond.async_respond import AsyncRespond
from slack_sdk.web.async_client import AsyncWebClient

from . import analyze_oom, route_bug, route_dev_env_infra_bug
from .knowledge import get_knowledge_channel_tools, get_knowledge_query_tools
from .sms import get_draft_sms_tool
from .task_list import get_enable_task_list_tools, get_task_list_write_tools
from .tools.athena_tools import get_execute_athena_query_tool
from .tools.python_tools import get_execute_python_tool
from .tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)
from .event_dedup import is_duplicate_event
from .common import (
    KST,
    answer,
    search_tool,
    get_web_page_from_url,
    get_create_notion_task_tool,
    get_update_notion_task_deadline_tool,
    get_update_notion_task_status_tool,
    get_create_notion_follow_up_task_tool,
    get_notion_page_tool,
)
from service.config import load_config, Squad
from service.slack_task_list import find_channel_task_list

# 상수들
# Notion API 2025-09-03 버전부터 data_source_id를 직접 사용
DATA_SOURCE_ID: str = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"  # 작업 DB (기본값)
PROJECT_DATA_SOURCE_ID: str = "1023943f-84d1-4223-a5a6-0c26e22d09f0"  # 프로젝트 DB

# 데이터 분석 지침. app/data_bot.py 시스템 프롬프트의 'Athena 데이터베이스 가이드'부터
# '파이썬 실행'까지와 Presto 문법 안내를 옮겨 적은 것이다.
# DB 안내든 도구 사용법이든 어느 쪽을 고치면 두 곳을 함께 고친다.
ROBOT_INSTRUCTIONS: str = (
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
    "- 쿼리 작성 시 Athena(Presto) SQL 문법을 사용합니다.\n"
)

# 유저그룹 멤버 캐시 (1시간 TTL)
_cache_usergroup_members: TTLCache = TTLCache(maxsize=20, ttl=3600)

# create_task 참조를 유지하여 GC 방지
_background_jobs: set[asyncio.Task] = set()


async def _get_user_squad(client: AsyncWebClient, user_id: str | None) -> Squad | None:
    """사용자가 속한 스쿼드를 결정합니다.

    각 스쿼드의 Slack usergroup 멤버 목록을 조회하여
    사용자가 포함된 첫 번째 스쿼드를 반환합니다.
    """
    if user_id is None:
        return None

    config = load_config()

    # 사용자별 오버라이드 확인 (복수 스쿼드 소속 시 우선 스쿼드 지정)
    if user_id in config.squad_overrides:
        return config.squad_overrides[user_id]

    for squad in config.squads:
        if squad.slack_usergroup_id is None:
            continue
        cache_key = f"usergroup_{squad.slack_usergroup_id}"
        if cache_key not in _cache_usergroup_members:
            try:
                resp = await client.usergroups_users_list(
                    usergroup=squad.slack_usergroup_id
                )
                _cache_usergroup_members[cache_key] = resp["users"]
            except Exception:
                # usergroup 조회 실패 시 빈 리스트로 처리
                _cache_usergroup_members[cache_key] = []
        if user_id in _cache_usergroup_members[cache_key]:
            return squad
    return None


async def _build_tools(
    client: AsyncWebClient,
    user_id: str | None,
    channel: str,
    thread_ts: str,
    say,
) -> list:
    """질문자의 스쿼드에 맞춘 도구 목록을 만듭니다.

    작업 생성 도구는 스쿼드별 Notion DB를 대상으로 하며,
    후속 작업 도구는 프로젝트/구성요소 속성이 있는 메인 DB에서만 사용합니다.

    작업 리스트로 등록된 채널은 예외입니다. 작업이 노션이 아니라 그 채널의
    슬랙 리스트로 가므로 노션 작업 생성 도구와 후속 작업 도구가 리스트 도구로
    바뀝니다. 후속 작업은 노션 작업에 딸리는 개념이라 함께 빠집니다.
    노션 작업을 조회하고 고치는 도구는 그대로 둡니다.

    Args:
        client: 슬랙 클라이언트
        user_id: 질문자의 Slack 사용자 ID
        channel: 채널 ID
        thread_ts: 스레드 타임스탬프
        say: 메시지 전송 함수. Athena 결과 표를 스레드에 올릴 때 쓴다.

    Returns:
        list: 에이전트에 주입할 도구 목록
    """
    slack_workspace = "monolith-keb2010"
    slack_thread_url = (
        f"https://{slack_workspace}.slack.com"
        f"/archives/{channel}/p{thread_ts.replace('.', '')}"
    )

    squad = await _get_user_squad(client, user_id)
    if squad and squad.notion_db and squad.notion_db.name != "main":
        task_ds_id = squad.notion_db.data_source_id
        title_prop = squad.notion_db.properties.title
        project_ds_id = None
    else:
        task_ds_id = DATA_SOURCE_ID
        title_prop = "제목"
        project_ds_id = PROJECT_DATA_SOURCE_ID

    # 작업 리스트로 등록된 채널은 작업을 노션이 아니라 그 리스트에 만든다.
    # 노션 작업 생성 도구를 같이 주면 에이전트가 둘 사이에서 흔들린다.
    #
    # 접속 실패는 노션 흐름으로 내려간다. 이 함수는 모든 답변이 지나는 길이라
    # 여기서 터지면 리스트와 무관한 기능까지 전부 무응답이 된다. 표가 없는
    # 경우(ProgrammingError)는 그대로 터뜨려 마이그레이션 누락을 드러낸다.
    try:
        task_list = await asyncio.to_thread(find_channel_task_list, channel)
        task_list_available = True
    except psycopg.OperationalError as error:
        print(f"작업 리스트 조회 실패, 노션으로 진행합니다: {error}")
        task_list = None
        task_list_available = False

    if task_list:
        create_tools = get_task_list_write_tools(
            client, channel, task_list, user_id, slack_thread_url
        )
    else:
        create_tools = [
            get_create_notion_task_tool(
                user_id,
                slack_thread_url,
                task_ds_id,
                client,
                project_ds_id,
                title_prop,
            )
        ]
        if project_ds_id:
            create_tools.append(get_create_notion_follow_up_task_tool(task_ds_id))
        # 조회가 실패한 동안에는 등록 도구를 주지 않는다. 이미 등록된 채널을
        # 미등록으로 보고 켜면 아무도 안 읽는 리스트가 하나 더 만들어진다.
        if task_list_available:
            create_tools += get_enable_task_list_tools(client, channel)

    notion_tools = [
        get_update_notion_task_deadline_tool(),
        get_update_notion_task_status_tool(task_ds_id),
        get_notion_page_tool(),
    ]

    draft_sms = get_draft_sms_tool(client, channel, thread_ts)

    return (
        [search_tool, get_web_page_from_url]
        + create_tools
        + notion_tools
        + get_knowledge_channel_tools(client, channel)
        + get_knowledge_query_tools(client, user_id)
        + [draft_sms]
        # 수백 행짜리 집계는 표를 컨텍스트에 실어 눈으로 세면 틀린다. 코드로 센다.
        # 차트는 슬랙에 올라가므로 클라이언트와 채널을 함께 넘긴다.
        + [
            get_execute_python_tool(
                thread_ts=thread_ts,
                slack_client=client,
                channel=channel,
                draft_sms=draft_sms.coroutine,
            )
        ]
        # 데이터 봇과 같은 데이터 분석 도구. 사용 지침은 ROBOT_INSTRUCTIONS 에 있다.
        + [
            list_redash_dashboards,
            read_redash_dashboard,
            read_redash_query,
            get_execute_athena_query_tool(
                say=say, thread_ts=thread_ts, slack_client=client, channel=channel
            ),
        ]
    )


def get_sms_revise(app):
    """문자 초안 수정 피드백을 초안을 쓴 에이전트에게 되돌리는 콜백을 만듭니다.

    카드의 [수정] 은 모달로 피드백만 받습니다. 그 피드백을 스레드의 다음
    질문처럼 다시 태워야 에이전트가 앞의 대화(수신자·문안·의도)를 그대로 두고
    문안만 고쳐 새 초안을 올립니다.

    app/sms.py 가 아니라 여기서 만듭니다. 저쪽에서 이 모듈을 부르면
    general → sms → general 로 순환합니다.

    Args:
        app: slack_bolt AsyncApp

    Returns:
        `async (channel, thread_ts, user, text) -> None` 콜백
    """

    async def revise(channel: str, thread_ts: str, user: str, text: str) -> None:
        async def say(payload, thread_ts=thread_ts):
            await app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=payload.get("text", "문자 초안을 다시 썼습니다"),
                **{k: v for k, v in payload.items() if k != "text"},
            )

        tools = await _build_tools(app.client, user, channel, thread_ts, say)
        await answer(
            thread_ts,
            channel,
            user,
            text,
            say,
            app.client,
            tools,
            ROBOT_INSTRUCTIONS,
        )

    return revise


SLACK_DAILY_SCRUM_CHANNEL_ID = "C02JX95U7AP"
SLACK_DAILY_SCRUM_CANVAS_ID = "F05S8Q78CGZ"
SLACK_BUG_REPORT_CHANNEL_ID = "C07A5HVG6UR"
SLACK_DEV_ENV_INFRA_BUG_CHANNEL_ID = "C096HGFDFM1"

# 버그 신고 채널은 제품별로 나뉘어 있으므로, 채널이 곧 제품이다.
# 값은 service.teams.PRODUCT_USERGROUP_IDS 의 키와 같아야 한다.
BUG_REPORT_CHANNEL_TO_PRODUCT = {
    SLACK_BUG_REPORT_CHANNEL_ID: "코들",
}

USER_ID_TO_LAST_HUDDLE_JOINED_AT = {}


async def _run_wa_job(
    func,
    args: list[str],
    kwargs: dict,
    description: str,
    respond: AsyncRespond,
) -> None:
    """
    `/wa` 작업을 스레드에서 실행하고 결과를 실행한 사람에게만 보여줍니다.

    ack 이후에는 응답 경로가 response_url 뿐이라, 예외를 여기서 잡지 않으면 실패가 파드
    로그에만 남고 사용자에게는 "진행 중" 에서 멈춘 것처럼 보인다.

    Args:
        func: 작업 함수
        args: 작업 함수의 위치 인자
        kwargs: 작업 함수의 키워드 인자
        description: 작업 표의 설명. 실패 안내에 쓴다
        respond: 슬래시 커맨드의 response_url 응답 함수
    """
    try:
        result = await asyncio.to_thread(func, *args, **kwargs)
    except Exception as error:
        traceback.print_exc()
        await respond(f":x: {description} 에 실패했습니다.\n```{error}```")
        return
    if isinstance(result, str):
        await respond(result)


def register_general_handlers(app):
    """
    범용 봇의 이벤트 핸들러를 등록합니다.
    """

    @app.event("app_mention")
    async def app_mention(body, say):
        """
        슬랙에서 로봇을 멘션하여 대화를 시작하면 호출되는 이벤트
        """
        if is_duplicate_event(body):
            return

        event = body.get("event")

        if event is None:
            return

        # 봇이 보낸 메시지는 무시 (자기 자신을 태그하는 무한 루프 방지)
        if event.get("bot_id"):
            return

        thread_ts = event.get("thread_ts") or body["event"]["ts"]
        channel = event["channel"]
        user = event.get("user")
        text = event["text"]

        # OOM 분석 요청 감지 (특정 채널의 스레드에서 "분석" 키워드 멘션)
        # 예: "@봇 분석해줘", "@봇 이 알림 분석해주세요"
        if channel == "C07B6FT3R5L" and "분석" in text and event.get("thread_ts"):
            await analyze_oom.analyze_oom_alert(app.client, body, say)
            return

        tools = await _build_tools(app.client, user, channel, thread_ts, say)
        await answer(
            thread_ts,
            channel,
            user,
            text,
            say,
            app.client,
            tools,
            ROBOT_INSTRUCTIONS,
        )

    @app.event("message")
    async def message(body, say):
        """
        DM으로 받은 질문에 답하고,
        버그 신고 채널에 올라오는 메시지를 LLM으로 분석하여
        Notion에 버그 작업을 생성하고, 시급한 경우 담당 그룹을 태그합니다.
        """
        if is_duplicate_event(body):
            return

        event = body.get("event", {})
        channel = event.get("channel")

        if event.get("channel_type") == "im":
            # 봇 메시지와 편집·삭제 등 서브타입 이벤트는 무시
            if event.get("bot_id") or event.get("subtype"):
                return

            thread_ts = event.get("thread_ts") or event["ts"]
            user = event.get("user")
            tools = await _build_tools(app.client, user, channel, thread_ts, say)
            await answer(
                thread_ts,
                channel,
                user,
                event["text"],
                say,
                app.client,
                tools,
                ROBOT_INSTRUCTIONS,
            )
        elif channel in BUG_REPORT_CHANNEL_TO_PRODUCT:
            # 메시지 편집 이벤트 필터링
            subtype = event.get("subtype")
            print(f"Subtype: {subtype}")
            if subtype != "bot_message":
                print("Skipping non-bot message")
                return

            thread_ts = event.get("thread_ts")
            message_ts = event.get("ts")
            print(f"Thread TS: {thread_ts}, Message TS: {message_ts}")

            if thread_ts is None or thread_ts == message_ts:
                print("Routing bug report")
                await route_bug.route_bug(
                    app.client, body, BUG_REPORT_CHANNEL_TO_PRODUCT[channel]
                )
        elif channel == SLACK_DEV_ENV_INFRA_BUG_CHANNEL_ID:
            # 메시지 편집 이벤트 필터링
            subtype = event.get("subtype")
            print(f"Subtype: {subtype}")
            if subtype != "bot_message":
                print("Skipping non-bot message")
                return

            thread_ts = event.get("thread_ts")
            message_ts = event.get("ts")
            print(f"Thread TS: {thread_ts}, Message TS: {message_ts}")

            if thread_ts is None or thread_ts == message_ts:
                print("Routing dev env infra bug report")
                await route_dev_env_infra_bug.route_dev_env_infra_bug(app.client, body)

    # 자동화 작업 표 — 단일 진입 커맨드 `/wa <작업>` 로 라우팅한다.
    # 슬랙 앱 UI 에는 `/wa` 하나만 등록하면 되고, 새 작업은 이 표에 한 줄만 추가한다.
    # 각 튜플: (작업명, module_path, func_name, description[, body_kwargs])
    # 작업명 뒤에 남은 토큰은 함수의 위치 인자로 전달한다.
    # 예: `/wa reset-develop jce-class-rails` → func("jce-class-rails")
    # body_kwargs 는 선택사항으로, body 에서 값을 꺼내 함수 키워드 인자로 전달할 매핑이다.
    # 예: {"caller_slack_user_id": "user_id"} → func(caller_slack_user_id=body.get("user_id"))
    # 함수가 문자열을 반환하면 실행한 사람에게만 보이는 응답으로 되돌려준다.
    _JOBS = [
        (
            "validate-customer-reports",
            "scripts.validate_customer_reports",
            "main",
            "고객 보고서 검증",
        ),
        (
            "manage-tasks-daily",
            "scripts.manage_tasks_daily",
            "main",
            "일일 작업 알림 처리",
        ),
        (
            "notify-upcoming-workevent",
            "scripts.notify_upcoming_workevent",
            "main",
            "근태 예정 알림 생성",
        ),
        (
            "notify-worktime-left",
            "scripts.notify_worktime_left",
            "main",
            "잔여 근무시간 계산",
        ),
        (
            "collect-review-stats",
            "scripts.collect_review_stats",
            "main",
            "리뷰 통계 수집",
        ),
        (
            "collect-coding-rule-feedbacks",
            "scripts.collect_coding_rule_feedbacks",
            "main",
            "코딩 규칙 피드백 수집",
        ),
        (
            "post-scrum-message",
            "scripts.post_scrum_message",
            "main",
            "스크럼 메시지 발송",
        ),
        (
            "schedule-scrum-mention",
            "scripts.schedule_scrum_mention",
            "main",
            "스크럼 멘션 발송",
        ),
        (
            "summarize-deployment",
            "app.summarize_deployment",
            "summarize_deployment",
            "배포 요약을 작성",
            {"caller_slack_user_id": "user_id"},
        ),
        (
            "crawl-education-bids",
            "scripts.crawl_education_bids",
            "main",
            "교육 외주 입찰공고 수집·평가",
        ),
        (
            "reset-develop",
            "scripts.reset_develop",
            "main",
            "develop 을 main 으로 초기화",
            {"caller_slack_user_id": "user_id"},
        ),
    ]

    _JOB_BY_SUB = {
        sub: (mod, fn, desc, rest[0] if rest else None)
        for sub, mod, fn, desc, *rest in _JOBS
    }

    def _wa_usage():
        lines = ["사용법: `/wa <작업> [인자]`", "", "작업 목록:"]
        lines += [f"• `{sub}` — {meta[2]}" for sub, meta in _JOB_BY_SUB.items()]
        return "\n".join(lines)

    @app.command("/wa")
    async def handle_wa(ack, body, respond):
        text = (body.get("text") or "").strip()
        sub = text.split()[0] if text else ""
        if sub in ("", "help", "list"):
            await ack(text=_wa_usage())
            return
        meta = _JOB_BY_SUB.get(sub)
        if not meta:
            await ack(text=f"알 수 없는 작업: `{sub}`\n\n{_wa_usage()}")
            return
        module_path, func_name, description, body_kwargs = meta
        await ack(text=f"⏳ {description} 중입니다…")
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        args = text.split()[1:]
        kwargs = {kw: body.get(bk) for kw, bk in (body_kwargs or {}).items()}
        # 슬래시 커맨드 응답은 리스너가 끝나야 나가므로, 작업을 기다리면 3초 제한에 걸려
        # ack 대신 타임아웃이 뜬다. 작업 결과와 실패는 _run_wa_job 이 response_url 로 보낸다.
        task = asyncio.create_task(
            _run_wa_job(func, args, kwargs, description, respond)
        )
        _background_jobs.add(task)
        task.add_done_callback(_background_jobs.discard)
