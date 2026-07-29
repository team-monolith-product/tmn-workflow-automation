# tmn-workflow-automation
업무 절차를 자동화하기 위한 도구 모음

## 구성 요소

### 1. Slack Bot (`app.py`)
Slack 채널에서 자동화 명령을 수신하고 처리하는 봇 서버

### 2. FastAPI Webhook Server (`main.py`)
Notion 등 외부 서비스에서 발생한 이벤트를 수신하여 자동화 워크플로우를 실행하는 경량 웹훅 서버

### 3. Drive Bot (`app/drive_bot.py`)
Google Drive 자료를 직접 탐색하며 답하는 에이전트 봇. 슬랙에서 멘션하면 필요한 만큼 파일을 검색·열람하고, 정보가 부족하면 되묻고, 요청에 따라 문서를 생성하거나 수정한다.

- 모델: `gpt-5.4` (reasoning effort high)
- 파일 단위 도구: `search_drive_files`, `read_drive_file`, `write_drive_file`
- 시트 조회 도구: `read_sheet_range` (범위 생략 시 탭 목록)
- 노션 연동: `create_ops_task` (운영 DB에 업무 등록, 슬랙 스레드 자동 첨부)
- 읽기 지원 형식: Google 문서/스프레드시트/프레젠테이션, PDF, 텍스트 계열
- 대화 맥락은 슬랙 스레드를 그대로 사용한다 (별도 세션 저장소 없음)
- 채널 캔버스가 있으면 매 요청마다 읽어 시스템 프롬프트에 넣는다

채널 캔버스는 그 채널의 공통 맥락(규칙, 자주 쓰는 폴더·DB, 참고 자료 목록)을 담는 자리다.
도구로 읽게 하면 LLM 턴이 하나 늘고 모델이 건너뛸 수도 있어서, 핸들러가 스레드 조회와
병렬로 미리 읽어 주입한다. 캔버스가 없거나 조회에 실패하면 조용히 건너뛴다.

캔버스 본문을 돌려주는 전용 API가 없어 `files.list`가 함께 주는 `url_private_download`를
봇 토큰으로 내려받는다. 공식 문서에 없는 경로라 실제 워크스페이스에서 검증이 필요하다.

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

### Drive Bot 전용
- `SLACK_BOT_TOKEN_DRIVE` / `SLACK_APP_TOKEN_DRIVE`: Drive 봇 Slack 토큰
- `GOOGLE_SERVICE_ACCOUNT_JSON`: 서비스 계정 JSON
- `GOOGLE_DRIVE_FOLDER_ID`: 새 파일을 만들 기본 폴더 ID (공유 드라이브 하위)

서비스 계정에 필요한 스코프: `drive`(파일 검색·읽기·쓰기), `spreadsheets.readonly`(셀 범위 조회).

Slack 봇 스코프: `app_mentions:read`, `chat:write`, `users:read`, `users:read.email`,
`channels:history`(스레드 조회), `files:read`(채널 캔버스 조회).

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
