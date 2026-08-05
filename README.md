# tmn-workflow-automation
업무 절차를 자동화하기 위한 도구 모음

## 구성 요소

### 1. Slack Bot (`app.py`)
Slack 채널에서 자동화 명령을 수신하고 처리하는 봇 서버

### 2. FastAPI Webhook Server (`main.py`)
Notion 등 외부 서비스에서 발생한 이벤트를 수신하여 자동화 워크플로우를 실행하는 경량 웹훅 서버

## 환경 변수

### 공통
- `SLACK_BOT_TOKEN`: Slack Bot 토큰
- `SLACK_APP_TOKEN`: Slack App 토큰
- `OPENAI_API_KEY`: OpenAI API 키
- `NOTION_TOKEN`: Notion 통합 토큰
- 기타 서비스별 토큰 및 설정

### FastAPI 전용
- `WORKFLOW_AUTOMATION_API_KEY`: 웹훅 API 인증을 위한 API 키 (필수)

### 문자 발송 (뿌리오)
- `PPURIO_ACCOUNT`: 뿌리오 계정 ID
- `PPURIO_API_KEY`: 연동 인증키
- `PPURIO_SENDER`: 사전 등록된 발신번호
- `PPURIO_WEB_ID` / `PPURIO_WEB_PASSWORD`: 발송결과 확인용 웹 로그인 계정
- `PPURIO_WEB_LOGIN_URL` / `PPURIO_WEB_RESULT_URL` / `PPURIO_WEB_ID_SELECTOR` / `PPURIO_WEB_PW_SELECTOR`: 발송결과 페이지 주소·셀렉터 (기본값 보정용)

뿌리오는 **호출 IP 사전 등록**이 필요하고(미등록 시 `code 3003 invalid ip`),
**발송결과 조회 API가 없어** 최종 도달 여부는 Playwright 로 웹 발송결과 페이지를 읽어 확인한다.
셀렉터 기본값은 추정치이므로 최초 1회 보정이 필요하다:

```bash
python -m service.ppurio_result --dump   # tmp/ppurio_*.html, *.png 확인 후 셀렉터 환경변수 조정
```

#### 발송 흐름
1. 슬랙에서 봇에게 문자 발송을 요청하면, 봇이 **명단과 문안 초안**을 승인 카드로 올린다 (이 단계에서는 발송하지 않음)
2. **[발송] 버튼**을 누르거나 초안 카드에 **✅ 이모지**를 달면 승인된다 (대화로 "보내줘"라고 해도 동일)
3. 발송 후 웹 발송결과를 폴링해 **실패한 번호만 재발송**한다 (최대 3회차)
4. 성공/실패/미확정을 스레드에 최종 보고한다

Slack 앱 설정에 `reactions:read` 스코프와 `reaction_added` 이벤트 구독, Interactivity 활성화가 필요하다.

## 로컬 실행

### Slack Bot
```bash
python app.py
```

### FastAPI Server
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## FastAPI 웹훅 사용법

### 엔드포인트

#### `GET /health`
헬스 체크 엔드포인트 (Kubernetes liveness/readiness probe용)

```bash
curl http://localhost:8000/health
```

#### `POST /webhook`
자동화 워크플로우 트리거 엔드포인트

**인증**: `X-API-Key` 헤더에 `WORKFLOW_AUTOMATION_API_KEY` 값 전달

**요청 예시**:
```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key-here" \
  -d '{
    "action": "process_notion_page",
    "notion_page_id": "abc123",
    "data": {
      "key": "value"
    }
  }'
```

**응답 예시**:
```json
{
  "success": true,
  "message": "Webhook received successfully",
  "action": "process_notion_page",
  "notion_page_id": "abc123"
}
```

## 배포

본 애플리케이션은 `jce-service-helm/workflow-automation-slack` Helm Chart를 통해 배포됩니다.
- Slack Bot과 FastAPI 서버는 동일한 Docker 이미지를 사용하며, 서로 다른 CMD로 실행됩니다.
- ArgoCD를 통해 자동 배포됩니다.
