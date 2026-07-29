# tmn-workflow-automation
업무 절차를 자동화하기 위한 도구 모음

## 구성 요소

### 1. Slack Bot (`app.py`)
Slack 채널에서 자동화 명령을 수신하고 처리하는 봇 서버

### 2. FastAPI Webhook Server (`main.py`)
Notion 등 외부 서비스에서 발생한 이벤트를 수신하여 자동화 워크플로우를 실행하는 경량 웹훅 서버

### 3. Operate Bot (`app/operate_bot.py`)
운영 업무를 돕는 에이전트 봇. 슬랙에서 멘션하면 Drive 자료·노션 운영 DB·슬랙 과거 대화를 직접 뒤져 답하고, 정보가 부족하면 되묻고, 요청에 따라 문서를 만들거나 운영 업무를 등록한다.

- 모델: `gpt-5.4` (reasoning effort high)
- 파일 단위 도구: `search_drive_files`, `read_drive_file`, `write_drive_file`
- 시트 조회 도구: `read_sheet_range` (범위 생략 시 탭 목록)
- 과거 조회 도구: `search_ops_tasks` (노션 운영 DB), `search_channel_messages` (슬랙 채널)
- 노션 등록: `create_ops_task` (운영 DB에 업무 등록, 슬랙 스레드 자동 첨부)
- 읽기 지원 형식: Google 문서/스프레드시트/프레젠테이션, PDF, 텍스트 계열
- 대화 맥락은 슬랙 스레드를 그대로 사용한다 (별도 세션 저장소 없음)

**작업 공간**: `GOOGLE_DRIVE_FOLDER_ID`가 봇의 작업 공간이다. 검색·읽기·쓰기가 모두 그
폴더의 하위 트리로 제한된다. 제한은 프롬프트 지시가 아니라 코드에서 건다 — 검색 쿼리에는
하위 폴더 조건이 자동으로 AND로 붙고, 읽기·쓰기는 대상 파일의 상위 폴더가 범위 안인지
확인한 뒤에만 진행한다. 모델이 범위를 벗어날 방법이 없다. 비워두면 접근 가능한 Drive
전체를 다룬다.

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
Google 문서의 부분 수정 도구는 두지 않았다. Docs API의 편집 요청은 문자 인덱스 기반이라
LLM이 위치를 잘못 계산해도 오류 없이 엉뚱한 곳을 고칠 수 있다. 문서 수정은 read 후
전체를 다시 쓰는 방식을 쓴다.

**공유 드라이브 필수**: 서비스 계정은 스토리지 할당량이 없어 개인 My Drive에 파일을 만들 수 없다(`403 storageQuotaExceeded`). `GOOGLE_DRIVE_FOLDER_ID`는 반드시 공유 드라이브 하위 폴더여야 하며, 해당 공유 드라이브에 서비스 계정을 **콘텐츠 관리자** 이상으로 추가해야 한다. 읽기 전용으로 쓸 폴더는 서비스 계정 이메일에 공유하면 검색·열람이 가능하다.

## 환경 변수

### 공통
- `SLACK_BOT_TOKEN`: Slack Bot 토큰
- `SLACK_APP_TOKEN`: Slack App 토큰
- `OPENAI_API_KEY`: OpenAI API 키
- `NOTION_TOKEN`: Notion 통합 토큰
- 기타 서비스별 토큰 및 설정

### Operate Bot 전용
- `SLACK_BOT_TOKEN_OPERATE` / `SLACK_APP_TOKEN_OPERATE`: Operate 봇 Slack 토큰
- `GOOGLE_SERVICE_ACCOUNT_JSON`: 서비스 계정 JSON
- `GOOGLE_DRIVE_FOLDER_ID`: 작업 공간 폴더. Drive 링크를 그대로 넣어도 되고 ID만 넣어도 된다 (공유 드라이브 하위)

서비스 계정에 필요한 스코프: `drive`(파일 검색·읽기·쓰기), `spreadsheets.readonly`(셀 범위 조회).

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
