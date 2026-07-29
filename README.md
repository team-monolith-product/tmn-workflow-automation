# tmn-workflow-automation
업무 절차를 자동화하기 위한 도구 모음

## 구성 요소

### 1. Slack Bot (`app.py`)
Slack 채널에서 자동화 명령을 수신하고 처리하는 봇 서버

### 2. FastAPI Webhook Server (`main.py`)
Notion 등 외부 서비스에서 발생한 이벤트를 수신하여 자동화 워크플로우를 실행하는 경량 웹훅 서버

### 3. Operate Bot (`app/operate_bot.py`)
운영 업무를 돕는 에이전트 봇. 슬랙에서 멘션하면 Drive 자료·노션 운영 DB·슬랙 과거 대화를 직접 뒤져 답하고, 정보가 부족하면 되묻고, 후속 업무를 노션에 등록한다. Drive와 시트는 읽기만 한다.

- 모델: `gpt-5.4` (reasoning effort high)
- 파일 단위 도구: `search_drive_files`, `read_drive_file`
- 시트 조회 도구: `read_sheet_range` (범위 생략 시 탭 목록)
- 과거 조회 도구: `search_ops_tasks` (노션 운영 DB), `search_channel_messages` (슬랙 채널)
- 노션 등록: `create_ops_task` (운영 DB에 업무 등록, 슬랙 스레드 자동 첨부)
- 읽기 지원 형식: Google 문서/스프레드시트/프레젠테이션, PDF, 텍스트 계열
- 대화 맥락은 슬랙 스레드를 그대로 사용한다 (별도 세션 저장소 없음)

**작업 공간**: `GOOGLE_DRIVE_FOLDER_ID`가 봇의 작업 공간이다. 검색과 읽기가 그 폴더의
하위 트리로 제한된다. 제한은 프롬프트 지시가 아니라 코드에서 건다 — 검색 쿼리에는 하위
폴더 조건이 자동으로 AND로 붙고, 읽기는 대상 파일의 상위 폴더가 범위 안인지 확인한
뒤에만 진행한다. 모델이 범위를 벗어날 방법이 없다. 비워두면 접근 가능한 Drive 전체를
다룬다.

Drive 검색의 `'X' in parents`는 직계 자식만 매칭하고 재귀가 없어서, 루트에서 하위 폴더를
BFS로 훑어 ID 집합을 만든 뒤 캐시한다(10분). 폴더 200개·깊이 5에서 멈춘다. 상한에
걸리면 일부를 못 찾을 뿐, 범위 밖 파일이 새어 나오지는 않는다.

**과거 조회는 2단계**: 예전에 처리한 일이면 노션 운영 DB를, 그 외에는 슬랙 대화를 먼저
보고 안 나오면 Drive 문서를 본다. 노션은 본문 검색 API가 없고 평균 초당 3요청 제한이
있으며, 슬랙은 봇 토큰에 검색 API가 열려 있지 않고 `conversations.history`가 최상위
메시지만 돌려준다. 그래서 양쪽 다 값싼 수단으로 후보를 좁힌 뒤(노션은 속성 필터, 슬랙은
채널·기간) 소수만 깊이 읽고(노션은 페이지 본문, 슬랙은 스레드 답글), 일치한 부분만
돌려준다. 결과 크기를 입력 크기와 분리해 컨텍스트가 터지지 않게 한다.

시트를 통째로 읽으면 큰 파일에서 컨텍스트가 터지므로, 범위 단위 도구를 함께 둔다.

**Drive와 시트는 읽기 전용이다.** 파일 생성·수정 도구를 두지 않았다. 잘못 덮어쓰면
되돌리기 어렵고, 특히 스프레드시트는 파일 단위로 덮어쓰면 구조가 통째로 깨진다.
정리한 결과는 슬랙 답변이나 노션 운영 업무로 남긴다.

**폴더 공유**: `GOOGLE_DRIVE_FOLDER_ID` 폴더를 운영봇 서비스 계정 이메일에 공유하면
검색·열람이 된다(뷰어 권한으로 충분). 공유 드라이브라면 서비스 계정을 멤버로 추가한다.

## 환경 변수

### 공통
- `SLACK_BOT_TOKEN`: Slack Bot 토큰
- `SLACK_APP_TOKEN`: Slack App 토큰
- `OPENAI_API_KEY`: OpenAI API 키
- `NOTION_TOKEN`: Notion 통합 토큰
- 기타 서비스별 토큰 및 설정

### Operate Bot 전용
- `SLACK_BOT_TOKEN_OPERATE` / `SLACK_APP_TOKEN_OPERATE`: Operate 봇 Slack 토큰
- `GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE`: 운영봇 전용 서비스 계정 JSON.
  없으면 공용 `GOOGLE_SERVICE_ACCOUNT_JSON`으로 떨어진다
- `GOOGLE_DRIVE_FOLDER_ID`: 작업 공간 폴더. Drive 링크를 그대로 넣어도 되고 ID만 넣어도 된다 (공유 드라이브 하위)

운영봇 서비스 계정에 필요한 스코프: `drive.readonly`(파일 검색·읽기),
`spreadsheets.readonly`(셀 범위 조회). 둘 다 읽기 전용이다.

계정을 분리한 이유: 봇은 Drive 접근이 필요해 기존 스크립트가 쓰는 공용 계정
(`spreadsheets.readonly`)보다 권한이 넓다. 나눠 두면 봇에 준 권한이 다른 곳으로 번지지
않고, 문제가 생겼을 때 봇 계정만 회수할 수 있다. 기존 `get_worksheet_values`는
공용 계정을 그대로 쓰므로 `scripts/discord_post_completion_notice.py`는 영향을 받지 않는다.

Slack 봇 스코프: `app_mentions:read`, `chat:write`, `users:read`, `users:read.email`,
`channels:history`(스레드·채널 조회).
비공개 채널까지 보려면 `groups:history`가 추가로 필요하다.

`conversations.history`의 요청당 999개 한도는 내부용 앱 기준이다. 마켓플레이스 밖으로
배포되는 앱은 2025년 5월부터 분당 1요청·15개로 제한되지만 사내 앱은 해당하지 않는다.

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
