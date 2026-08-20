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

## 문자 발송 (뿌리오)

슬랙에서 봇에게 말하면 초안이 올라오고, **요청한 사람이 [보내기] 를 눌러야**
실제로 나갑니다.

```
@봇 이 번호들한테 디스코드 링크 안내 보내줘
   010-1111-1111 홍길동 / 010-2222-2222 김철수

  ↓ 봇이 스레드에 카드를 올린다 (아직 안 나감)

  문자 발송 확인
  discord안내 · 2명
  SMS · 치환 후 최대 62byte
  대상: 홍길동, 김철수
  미리보기: 홍길동선생님, 안내드립니다 …
  [보내기]  [취소]

  ↓ 요청자가 [보내기] → 확인 팝업 → 그때 발송
```

에이전트가 도구를 부르는 것만으로는 나가지 않습니다. 실제 사람에게 돈을 들여
나가는 것이라, 모델이 대화를 잘못 읽었을 때 되돌릴 방법이 없기 때문입니다.
승인은 **요청한 사람만** 할 수 있고, 두 번 눌러도 한 번만 나갑니다.

`campaign` 을 붙이면 같은 사람에게 두 번 가지 않습니다. 개인 CS 처럼 여러 번
보내는 게 정상이면 비워 둡니다.


- `PPURIO_ACCOUNT`: **ppurio.com 로그인 아이디**. 연동 페이지가 인증키만 발급하는 것은 계정이 이미 본인이기 때문이고, Basic 인증은 `Base64(계정:인증키)` 로 만듭니다
- `PPURIO_API_KEY`: 연동 관리 페이지에서 발급받는 API 인증키. 웹 로그인 비밀번호와 다른 값입니다
- `PPURIO_SENDER`: 계정에 사전등록된 발신번호. **하이픈 없는 숫자만** 넣으세요 — 값을 정규화하지 않고 그대로 벤더의 `from` 에 싣습니다
- 호출 IP 를 뿌리오에 사전 등록해야 합니다(미등록 시 `3003 invalid ip`). 운영 파드는 NAT 고정 EIP `3.37.41.32` 로 나갑니다

발송 기록은 `sms_send` 테이블에 남습니다. 한 사람에게 한 번 보낸 것이 한 행이고,
중복 차단은 부분 UNIQUE 인덱스가 합니다.

```
campaign  phone        content        claimed_at  sent_at   failed_at  confirmed_at
discord   01011111111  [*이름*]선생…  20:14       20:14     —          20:16
discord   01022222222  [*이름*]선생…  20:14       —         —          —      ← 모름
(NULL)    01011111111  안녕하세요…    09:30       09:30     —          —      ← 개인 CS
```

`campaign` 이 있으면 같은 사람에게 두 번 가지 않습니다. **개인 CS 는 `--cs` 로
보내며 `campaign` 이 NULL 이라 중복 차단을 받지 않습니다** — 같은 사람에게
여러 번 보내는 게 정상이기 때문입니다.

상태를 `status` 한 컬럼으로 두지 않고 **단계마다 시각을 남깁니다.** 그래야
도달 확인이 붙을 때 "언제 보냈는지"가 덮이지 않습니다.

- `sent_at`·`failed_at` 이 **둘 다 비어 있으면** 접수 여부를 모르는 상태입니다
  (타임아웃·5xx). 재시도가 막히고, 뿌리오 웹에서 확인한 뒤 사람이 풀어야 열립니다
- `failed_at` 은 벤더가 거절한 것이 확실해 재시도가 바로 열립니다
- `confirmed_at` 은 도달 확인이 채웁니다(아직 미구현)

보낸 문안은 `content`(치환 전 원문)와 `variables`(그 사람의 치환값)로 남아,
"이 사람이 실제로 받은 문자"를 그대로 되살릴 수 있습니다.

```bash
# 문안·타입·길이만 확인 (발송하지 않음)
python scripts/send_sms.py --campaign discord \
    --content "[*이름*]선생님, 안내드립니다" --to 010-… --name 홍길동 --dry-run

# 명단 파일로 (헤더: to,name,var1..var8)
python scripts/send_sms.py --campaign discord --content "..." --csv roster.csv

# 개인 CS — 중복 차단 없음
python scripts/send_sms.py --cs --content "..." --to 010-… --name 홍길동

# 그 번호에게 뭘 보냈는지
python scripts/send_sms.py --history 010-…
```

문안은 `--content` 로 바로 넣습니다. 치환은 뿌리오 태그를 그대로 씁니다 —
이름은 `[*이름*]`, 나머지는 `[*1*]`~`[*8*]`. 같은 문안을 반복해서 쓰게 되면
그때 `templates/sms/<이름>.txt` 를 만들어 `--template` 으로 부릅니다.

조회는 슬랙에서 봇에게 묻거나 Redash 로 봅니다. 시트에 기록을 남기지 않는
이유는 구글 시트에 조건부 쓰기가 없어 중복 차단을 원자적으로 못 하기
때문입니다 — 진입점이 늘면 겹치는 순간 같은 사람에게 두 번 나갑니다.
