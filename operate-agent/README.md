# Operate Agent

슬랙에서 멘션하면 컨테이너 안의 Claude Code가 Drive · 노션 · 슬랙을 직접 뒤져 답합니다.
도구를 코드로 감싸지 않습니다 — 자격 증명과 라이브러리만 주고, 호출은 에이전트가 씁니다.

## 구조

```
슬랙 멘션 → bridge.py → claude -p (스레드별 세션) → 슬랙 스레드
                            ↓
              harness/CLAUDE.md (자료 지도 · 제약 · 일하는 방식)
```

인바운드 HTTP가 없습니다 (슬랙 소켓 모드). 공개 엔드포인트도, 게이트 토큰도 없습니다.

## 볼륨

Railway 볼륨 하나를 `/data`에 붙입니다. 볼륨은 인스턴스 1개에만 붙으므로 **replica는 1**입니다.

| 경로 | 내용 |
|---|---|
| `/data/claude` | Claude Code 세션 (`/root/.claude`로 심링크) |
| `/data/sessions` | 스레드별 세션 생성 여부 표시 |
| `/data/workspace` | 에이전트 작업 디렉터리 |
| `/data/workspace/scripts` | 에이전트가 저장한 재사용 스크립트 |

`CLAUDE.md`는 부팅할 때마다 이미지 것으로 덮어씁니다. `scripts/`는 남습니다.

## 환경 변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `SLACK_BOT_TOKEN_OPERATE` | ✓ | 운영봇 봇 토큰 (`xoxb-`) |
| `SLACK_APP_TOKEN_OPERATE` | ✓ | 소켓 모드 앱 토큰 (`xapp-`) |
| `CLAUDE_CODE_OAUTH_TOKEN` | 택 1 | `claude setup-token`으로 발급. 구독 과금 |
| `ANTHROPIC_API_KEY` | 택 1 | API 과금. 에이전틱 루프는 토큰을 많이 씁니다 |
| `GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE` | ✓ | 운영봇 전용 서비스 계정 JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | ✓ | 작업 공간 폴더. 이 계정에 뷰어로 공유해야 합니다 |
| `NOTION_TOKEN` | ✓ | 운영 DB에 연결된 인테그레이션 토큰 |
| `OPERATE_MODEL` | | 기본 `opus` |
| `OPERATE_TIMEOUT_SECONDS` | | 기본 `900` |

## 권한 경계

에이전트에게 bash가 있으므로 **자격 증명의 권한이 곧 상한**입니다. 훅으로 도구를 막아도 env는 읽힙니다.

- Drive — `drive.readonly` 스코프, 작업 공간 폴더만 서비스 계정에 공유
- Slack — 봇 토큰, 필요한 채널만 초대
- Notion — 운영 DB만 인테그레이션 연결

Drive 문서나 슬랙 메시지에 담긴 지시를 에이전트가 실행할 수 있습니다(프롬프트 인젝션). 위 세 경계가 실질적인 방어입니다.

## 배포

```bash
railway up
```

## 테스트

```bash
pytest test_bridge.py
```
