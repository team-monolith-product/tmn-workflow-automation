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
- `PPURIO_SENDER`: 계정에 사전등록된 발신번호. 발신번호 사전등록제라 이 값 없이는 접수되지 않습니다
- `GOOGLE_SERVICE_ACCOUNT_JSON`: 참가자 명단 시트에 쓸 서비스 계정. `google-drive-bot@elegant-circle-503206-a1.iam.gserviceaccount.com` 을 쓰며, 그 계정이 대상 시트에 **편집자**로 공유되어 있어야 합니다(캠페인 열을 새로 만듭니다)

발송 기록은 참가자 명단 시트에 **캠페인마다 열 하나**로 남습니다. 사람이 보던 그
시트에서 누가 무엇을 받았는지 바로 보입니다.

| 성명 | 휴대폰 | … | discord안내 | 8월정산안내 |
|---|---|---|---|---|
| 홍길동 | 010-… | | 2026-08-11 20:14 | |
| 김철수 | 010-… | | 2026-08-11 20:14 | 2026-08-20 09:00 |

그 열이 빈 사람만 보냅니다. 사람이 손으로 아무 값이나 적어도 "보냈다"로 읽히므로,
장애 중에 뿌리오 웹으로 직접 보내고 표시하는 경로가 그대로 살아 있습니다.

**공식 문자만 기록합니다.** 개인 CS 문자는 슬랙 스레드가 기록입니다 — 같은 사람에게
여러 번 보내는 게 정상이라 "이미 보냈으니 빼자"는 판정 자체가 틀립니다.

```bash
# 문안·타입·길이만 확인
python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \
    --content "[*이름*]선생님, 안내드립니다" --csv roster.csv --dry-run

# 실제 발송
python scripts/send_sms.py --spreadsheet <시트주소> --campaign discord \
    --content "[*이름*]선생님, 안내드립니다" --csv roster.csv
```

문안은 반복해서 쓰면 `templates/sms/*.txt`, 일회성이면 `--content` 로 바로 넣습니다.
치환은 뿌리오 태그를 그대로 씁니다 — 이름은 `[*이름*]`, 나머지는 `[*1*]`~`[*8*]`.

`--spreadsheet` 는 주소창을 통째로 붙여넣으면 됩니다. 주소에 들어 있는 `gid` 로
보고 있던 탭을 그대로 엽니다. 탭 이름으로 지정하려면 `--worksheet`,
둘 다 없으면 첫 번째 탭입니다.

번호 열은 제목으로 찾습니다 — `휴대폰` · `연락처` · `전화번호` · `휴대전화` ·
`전화` · `번호` 중 하나가 열 제목에 들어 있으면 됩니다. 구글 폼 응답 시트의
`휴대전화 번호` 같은 문항 제목도 그대로 잡힙니다. 못 찾으면 발송하지 않고
거절합니다.
