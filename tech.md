# 데이터 분석 슬랙 봇 시스템 설계

> **문서 목적**: AI가 추론 불가능한 프로젝트별 특수 컨텍스트만 제공
> **포함**: 인간의 의사결정, 기존 코드베이스 구조, 특수 비즈니스 로직
> **배제**: AI가 추론 가능한 모든 것 (구현 방법, 베스트 프랙티스, 테스트, 에러 처리 등)

## 1. spec.md와의 차이점 (인간의 의사결정)

**원래 요구사항**:
- 핵심 분석 Agent는 Claude Code CLI 사용
- Redash는 모범 쿼리 사례 참고

**변경된 설계**:
- **GPT 5.2 사용**: 현재 시스템이 OpenAI 기반, 일관성 유지
- **Redash Dashboard Tools**: 프로덕션에서 검증된 쿼리만 사용 (임의 쿼리 배제)
- **Routing 시스템**: Tool 8-9개는 LLM 성능 저하 → 3개씩 분리

## 2. 현재 시스템 (기존 코드베이스)

```
app/
├── general.py          # @app.event("app_mention") 핸들러
├── contents.py         # 콘텐츠팀 전용 핸들러
└── common.py           # answer() 함수, create_react_agent

현재 모델: OpenAI GPT-4.1
현재 Tools: Notion, Tavily Search, WebPage
```

환경 변수: `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `NOTION_TOKEN` (이미 설정됨)

## 3. 새로운 아키텍처

### 파일 구조

```
app/
├── router.py           # [NEW] 질문 분류 (data_analysis | general)
├── data_analysis.py    # [NEW] 데이터 분석 Agent
└── tools/
    ├── athena_tools.py # [NEW] execute_athena_query
    └── redash_tools.py # [NEW] list_redash_dashboards, read_redash_dashboard
api/
├── athena.py           # [NEW] boto3 wrapper
└── redash.py           # [NEW] GET /api/dashboards, GET /api/dashboards/{slug}
```

### 환경 변수 (추가)

```bash
# AWS Athena
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ap-northeast-2
ATHENA_OUTPUT_LOCATION=s3://your-bucket/athena-results/

# Redash
REDASH_API_KEY=...
REDASH_BASE_URL=https://your-redash-instance.com
```

## 4. 핵심 설계 결정

### Router 분류 기준

```python
async def route_question(question: str) -> Literal["data_analysis", "general"]:
    """
    data_analysis: 사내 지표, 데이터 분석, SQL, 시계열, 비즈니스 지표
    general: 노션, 검색, 일반 대화
    """
```

### Redash 사용 방식

**왜 Redash Dashboard를 사용하는가**:
- 프로덕션 검증된 쿼리만 사용 (임의 쿼리는 품질 낮음)
- 대시보드 = 큐레이션된 모범 사례

**Tools**:
1. `list_redash_dashboards(query: str | None)` → 동적 검색
2. `read_redash_dashboard(slug: str)` → 쿼리 정의, 데이터베이스, 테이블 스키마, JOIN 패턴
3. `execute_athena_query(query: str, database: str)` → SQL 실행

### 핸들러 통합

```python
@app.event("app_mention")
async def app_mention(body, say):
    agent_type = await route_question(text)

    if agent_type == "data_analysis":
        tools = [list_redash_dashboards, read_redash_dashboard, execute_athena_query]
        # GPT 5.2, System: "데이터 분석 전문가"
    else:
        # 기존 answer() 함수 사용
```

---

**버전**: 4.0
**작성**: 2025-12-25
