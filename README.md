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

### 문자 발송 (뿌리오)
- `PPURIO_ACCOUNT` / `PPURIO_API_KEY`: 발송 API 계정·인증키. 호출 IP 를 뿌리오에 사전 등록해야 합니다(미등록 시 토큰 발급에서 `3003 invalid ip`)
- `GOOGLE_SERVICE_ACCOUNT_JSON`: 발송이력을 적을 참가자 스프레드시트에 쓸 서비스 계정

발송 이력은 참가자 스프레드시트의 `발송이력` 탭에 한 발송이 한 행으로 쌓입니다.
같은 `campaign` 으로 이미 보낸 번호는 자동으로 빠지므로 같은 명령을 여러 번 돌려도
같은 사람에게 두 번 가지 않습니다.

```bash
# 문안·타입·길이만 확인
python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \
    --template discord --csv roster.csv --dry-run

# 실제 발송
python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \
    --template discord --csv roster.csv
```

문안은 반복해서 쓰면 `templates/sms/*.txt`, 일회성이면 `--content` 로 바로 넣습니다.
치환은 뿌리오 태그를 그대로 씁니다 — 이름은 `[*이름*]`, 나머지는 `[*1*]`~`[*8*]`.
