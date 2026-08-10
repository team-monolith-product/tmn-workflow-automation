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
- `PPURIO_ACCOUNT` / `PPURIO_API_KEY` / `PPURIO_SENDER`: 발송 API 계정·인증키·발신번호
- `PPURIO_WEB_ID` / `PPURIO_WEB_PASSWORD`: 도달 결과 확인용 웹 로그인 계정
- `PPURIO_WEB_LOGIN_URL` / `PPURIO_WEB_RESULT_URL` / `PPURIO_WEB_ID_SELECTOR` / `PPURIO_WEB_PW_SELECTOR`: 발송결과 페이지 주소·셀렉터 (기본값 보정용)

호출 IP 를 뿌리오에 사전 등록해야 합니다(미등록 시 `code 3003 invalid ip`).

#### 흐름
1. 데이터를 조회해 명단을 만들고, 봇이 `draft_sms` 로 **명단·문안 승인 카드**를 올립니다 (발송 안 함)
2. 사람이 **[발송] 버튼**이나 **✅ 이모지**로 승인합니다
3. `UNIQUE (campaign, phone)` 로 이미 보낸 번호를 빼고 **한 번의 API 호출로 전원 발송**합니다
4. 웹 발송결과를 폴링해 `result_code` 를 채우고, **도달 실패분만** `campaign-r2` 로 재발송합니다(최대 3회차)

문안은 반복해서 쓰면 `templates/sms/*.txt`, 일회성이면 대화에서 바로 작성합니다.

#### 도달 확인 (Playwright)
뿌리오 v1 에는 결과 조회 API 가 없어 웹 페이지를 브라우저로 읽습니다. 주소·셀렉터
기본값은 추정치이므로 최초 1회 보정이 필요합니다.

```bash
python -m service.sms.result --dump   # tmp/ppurio_*.html, *.png 확인
```

헤드리스로 돌지만 컨테이너에서는 `--no-sandbox`(root 실행)와
`--disable-dev-shm-usage`(/dev/shm 64MB)가 필요하며 코드에 반영돼 있습니다.
파드 메모리는 봇 프로세스와 Chromium 이 함께 올라갈 여유가 있어야 합니다
(jce-service-helm#608 에서 2Gi 로 상향).

Slack 앱에 `reactions:read` 스코프와 `reaction_added` 이벤트 구독, Interactivity 활성화가 필요합니다.

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
