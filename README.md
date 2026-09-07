# tmn-workflow-automation
업무 절차를 자동화하기 위한 도구 모음

## 구성 요소

### 1. Slack Bot (`app.py`)
Slack 채널에서 자동화 명령을 수신하고 처리하는 봇 서버

### 2. FastAPI Webhook Server (`main.py`)
Notion 등 외부 서비스에서 발생한 이벤트를 수신하여 자동화 워크플로우를 실행하는 경량 웹훅 서버

### 3. Operations Slack Task MCP (`main.py`)
운영팀 Slack List 작업의 생성·시작·재개, 상태·요청 맥락·이전 작업 기록 조회, 종료 결과 게시를 처리합니다. Knowledge MCP와 같은 FastAPI 프로세스에서 `/mcp/operate` 경로를 제공합니다.

### 4. TMN Operating Plugin
사내 플러그인 원문은 비공개 Marketplace에서 관리합니다. Codex·Claude에서 다음 HTTPS Git Marketplace URL을 최초 한 번 등록한 뒤 `TMN Operating`을 설치합니다.

```text
https://wfa.codle.io/plugins/tmn-operating.git
```

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

Operations Slack Task MCP도 같은 서버에서 실행됩니다. 로컬 주소는 `http://localhost:8000/mcp/operate`이고, 운영 환경에서는 `https://wfa.codle.io/mcp/operate`입니다.

필수 환경 변수는 `ADMIN_RAILS_BASE_URL`, `KNOWLEDGE_DATABASE_URL`, `MCP_RESOURCE_URL`, `SLACK_BOT_TOKEN`입니다. 운영 환경의 `MCP_RESOURCE_URL`은 경로를 제외한 `https://wfa.codle.io`이며 두 MCP가 공유합니다. 기존 `KNOWLEDGE_MCP_RESOURCE_URL`은 `MCP_RESOURCE_URL`로 이름을 바꿉니다. Operations MCP는 기존 Team Monolith Slack 봇 토큰(`SLACK_BOT_TOKEN`)을 사용하며, 별도 이메일 허용 목록 없이 admin-rails 인증에 성공한 사내 계정을 허용합니다. `KNOWLEDGE_DATABASE_URL`은 같은 Slack List 행의 작업 스레드가 동시에 두 개 생기지 않도록 advisory lock을 잡는 데만 쓰며, 작업과 스레드의 관계는 저장하지 않습니다.

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
- Operations Slack Task MCP는 Knowledge MCP와 같은 FastAPI 서비스에서 실행합니다. Ingress는 `/mcp`와 `/mcp/operate`를 같은 서비스로 전달합니다.
- ArgoCD를 통해 자동 배포됩니다.

## 문자 발송 (뿌리오)

슬랙에서 봇에게 말하면 초안 카드가 올라오고, **[보내기] 를 눌러야** 나갑니다.

```
@봇 이 번호들한테 디스코드 링크 안내 보내줘
   010-1111-1111 홍길동 / 010-2222-2222 김철수

  ↓ 봇이 스레드에 카드를 올린다 (아직 안 나감)

  문자 발송 확인 — 2명 · SMS
  [*이름*]선생님, 안내드립니다

  치환값
  번호           이름
  01011111111    홍길동
  01022222222    김철수

  [보내기]  [취소]
```

카드는 치환 후 문장이 아니라 태그가 살아 있는 원문과 치환값 목록을 보여줍니다.
벤더로 나가는 것이 그것이고, 태그가 있어야 할 자리에 이름이 박혀 있는 실수도
그래야 보입니다.

캠페인 전체가 요청 1회입니다. 문안 하나에 수신자 배열을 실으면 이름·기수는
벤더가 치환합니다 — 이름은 `[*이름*]`, 나머지는 `[*1*]`~`[*8*]`.

슬랙 없이 발송 계층만 확인하려면:

```bash
python scripts/send_sms.py --content "[*이름*]선생님, 안내드립니다" \
    --to 010-1111-1111 --name 홍길동 --dry-run
```

### 환경 변수

- `PPURIO_ACCOUNT`: **ppurio.com 로그인 아이디**. 연동 페이지가 인증키만 발급하는 것은 계정이 이미 본인이기 때문이고, Basic 인증은 `Base64(계정:인증키)` 로 만듭니다
- `PPURIO_API_KEY`: 연동 관리 페이지에서 발급받는 API 인증키
- `PPURIO_SENDER`: 계정에 사전등록된 발신번호. **하이픈 없는 숫자만** 넣으세요 — 값을 정규화하지 않고 그대로 벤더의 `from` 에 싣습니다
- 호출 IP 를 뿌리오에 사전 등록해야 합니다(미등록 시 `3003 invalid ip`). 운영 파드는 NAT 고정 EIP `3.37.41.32` 로 나갑니다

버튼이 동작하려면 Slack 앱 설정에서 **Interactivity & Shortcuts** 를 켜야 합니다
(Socket Mode 라 Request URL 은 필요 없습니다).

발송 기록을 DB 에 남기는 것과 도달 확인은 다음 PR 입니다.
