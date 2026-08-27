# 구글폼 생성

선생님에게 정보를 받는 가장 쉬운 통로가 구글폼이다. 슬랙에서 "이런 폼 만들어줘"라고 말하면
봇이 만들어 공개하고 사람이 마지막 두 번 클릭할 링크를 같이 준다.

지금은 이 일이 `repo/industry-linked/create_*_form.py` 일곱 개에 복붙으로 흩어져 있고
**공개 처리를 둘 다 갖춘 것은 넷뿐이다.**

| 스크립트 | 링크 공개 | 게시 |
|---|---|---|
| demand / jitda_event / team / visit | O | O |
| staff_doc | O | **X** |
| survey | **X** | O |
| short_term | **X** | **X** |

공개를 부르는 쪽의 선택지로 두는 한 이 표는 계속 나온다.

## 누가 무엇을 하나

| | 누가 | 왜 |
|---|---|---|
| 폼 만들기 · 문항 채우기 | 봇 | `forms.create`가 SA 에서 500 이라 Drive 로 우회한다 |
| 링크 공개 + 게시 | 봇 | 빠뜨리면 응답이 0건이다 |
| 편집자 공유 | 봇 | 없으면 사람이 아래 둘을 누를 수 없다 |
| **응답 시트 연결** | **사람** | API 에 스위치가 없다. 폼 화면에서 누르면 실시간으로 붙는다 |
| **`forms.gle` 단축 링크** | **사람** | API 에 없다. 폼 화면 '보내기'에서 받는다 |

아래 둘을 흉내 내지 않는다. 우리가 응답을 퍼 나르면 지연이 생기고 우리 도메인으로 단축을
발급하면 엔드포인트와 표가 는다. 사람이 누르면 구글이 진짜로 붙여 주고 진짜 `forms.gle`을 준다.

## 공개는 두 겹이고, 이제 빠뜨리면 무조건 터진다

링크 공개(Drive `permissions.create` `anyone`/`reader`)와 게시(Forms `setPublishSettings`
`isPublished`+`isAcceptingResponses`)는 별개다. 하나만 해도 응답이 0건이다.

**2026-07-01 부터 API 로 만든 폼은 기본이 미게시다.** 그 전에는 하위 호환으로 게시된 채
만들어졌다. 오늘(8/27)은 이미 지난 뒤라 게시를 빼먹은 폼은 예외 없이 응답을 못 받는다.
`staff_doc`·`short_term` 방식은 지금 그대로 돌리면 죽는다.

## 설계

### 1. 순서가 곧 안전장치다

```
① files.create        전용 공유 드라이브 폴더에 폼 셸을 만든다
② deleteItem × N      셸에 딸려 온 기본 문항을 역순으로 지운다
③ batchUpdate         제목·설명·문항을 넣는다
④ permissions.create  편집자를 붙인다
⑤ setPublishSettings  게시한다
⑥ permissions.create  anyone/reader 로 링크를 연다
⑦ 되읽기 검증         ④⑤⑥ 을 전부 확인한다
```

**②를 빼먹으면 만드는 폼마다 1번 문항이 "제목 없는 질문"이다.** Drive 우회로 만든 셸에는
기본 문항이 딸려 온다. `deleteItem`이 뒤 인덱스를 당기므로 **역순으로** 지운다. 정순으로
지우면 절반이 남는다. 일곱 스크립트가 전부 이렇게 한다.

**링크 공개를 맨 뒤에 둔다.** 중간에 죽으면 남는 폼이 비공개다. 편집자 공유가 실패했는데
공개까지 끝나 있으면 아무도 주소를 모르는 공개 폼이 드라이브에 쌓인다. 폼 삭제 도구는
만들지 않으므로 치울 방법도 없다. 어느 단계에서 죽든 **예외 메시지에 formId 와 편집 링크를
싣는다.**

### 2. 공개는 생성의 일부다

`create_form()`이 ①~⑦ 을 마친 뒤에야 링크를 돌려준다. 실패하면 예외를 던지며 인자로 끄고
켜지 못한다.

**편집자 공유도 검증에 넣는다.** 이것만 실패하면 폼은 만들어졌는데 사람이 ①②를 누를 수
없고 그 상태로 "만들었습니다" 안내가 나간다.

```python
# 공유 드라이브 멤버십은 파일 권한에 organizer·fileOrganizer 로 상속돼 내려온다.
# writer 만 세면 그 폴더를 관리하는 사람이 "공유 실패"로 잡힌다.
WRITE_ROLES = {"writer", "fileOrganizer", "organizer"}

perms = drive.permissions().list(
    fileId=fid, fields="permissions(type,role,emailAddress)",
    supportsAllDrives=True).execute()["permissions"]
assert any(p["type"] == "anyone" and p["role"] == "reader" for p in perms), "링크 공개가 안 됐다"
opened = {p.get("emailAddress", "").lower() for p in perms if p["role"] in WRITE_ROLES}
missing = {e.lower() for e in editors} - opened
assert not missing, f"편집자 공유 실패: {missing}"
state = svc.forms().get(formId=fid).execute().get("publishSettings", {}).get("publishState", {})
assert state.get("isPublished") and state.get("isAcceptingResponses"), "게시가 안 됐다"
```

④ 에서도 같은 이유로 **부여 전에 `permissions.list` 를 먼저 읽고 없는 사람만 `create`
한다.** 공유 드라이브는 상속된 역할보다 낮은 파일 단위 권한 부여를 거부하므로, 이미 콘텐츠
관리자인 사람에게 `writer` 를 주려 들면 그 호출 자체가 실패한다.

`.get`을 겹쳐 쓰는 이유는 `publishSettings`가 없을 때 `KeyError`가 나면 위 한국어 실패
메시지가 안 나오기 때문이다.

`create_jitda_event_form`의 공유·이동은 `except Exception: pass`인데 **그건 옮기지 않는다.**
AGENTS.md 가 예외를 삼키지 말라고 못 박았고 삼키면 지금 검증하려는 실패가 정확히 숨는다.

### 3. 남은 두 번은 링크로 넘긴다

무엇을 눌러야 하는지까지 답에 박아 준다. 응답 링크는 `forms.get`이 주는 `responderUri`를
그대로 쓴다. **그 URL 의 ID 는 `formId`와 다른 값이라 조합해서 만들 수 없다.**

```
폼을 만들었습니다. 링크를 뿌리기 전에 두 가지만 눌러 주세요.

  ① 응답 시트 연결  https://docs.google.com/forms/d/{formId}/edit#responses
     '응답' 탭 → 시트 아이콘 → [새 스프레드시트 만들기]

  ② 짧은 주소 받기  https://docs.google.com/forms/d/{formId}/edit
     오른쪽 위 [보내기] → 링크 아이콘 → 'URL 단축' 체크 → 복사

  공개 상태 : 링크 공개 O · 게시 O
  편집자   : byb@team-mono.com, chk@team-mono.com
  응답 링크 : {responderUri}
```

②가 필요한 이유는 길이다. 원본 응답 링크는 100자라 EUC-KR 90바이트인 문자 한 통에 안
들어가서 단축 없이 보내면 LMS 로 넘어가 통당 단가가 오른다.

**편집자 목록을 안내문에 같이 찍는다.** 부른 사람이 그 목록에 없으면 ①②가 권한 없음으로
막히는데 목록이 보이면 그 자리에서 안다.

### 4. 편집자는 사내 주소만, 최소 한 명

편집자 인자는 LLM 이 채운다. 제한이 없으면 슬랙에서 "이 주소도 편집자로 넣어줘" 한 마디로
아무 주소나 writer 가 된다. 폼 writer 는 **응답 전량**을 본다. 성함·소속 학교·연락처다.
게다가 기존 스크립트처럼 `sendNotificationEmail=False`로 붙이면 붙은 당사자에게도 메일이
가지 않아 슬랙 스레드 밖에서는 드러나지 않는다.

- 주소는 소문자로 바꾼 뒤 **`@team-mono.com` 접미사**로 판정한다. `endswith("team-mono.com")`
  으로 짜면 `evil-team-mono.com` 이 통과한다.
- **편집자가 한 명도 없으면 거부한다.** 검증의 `missing` 은 목록이 비면 항상 빈 집합이라
  그대로 통과하고 아무도 못 여는 폼이 "검증 통과"로 나간다.
- 기본값은 `FORM_EDITORS`(쉼표 구분)에서 읽는다. 외부 편집자가 필요하면 그때 사람이 적는다.

### 5. 계정과 폴더

`FORM_SERVICE_ACCOUNT_JSON`을 새로 판다. 스코프는 `drive`·`forms.body` 다.

**폼 전용 공유 드라이브를 따로 파고 이 계정을 거기에만 넣는다.** `drive.file` 로 좁히면
경계가 스코프로 지켜지지만 그 스코프는 앱이 만든 파일만 열기 때문에 앱 바깥에서 만든
폴더를 `parents` 로 주는 생성이 통과하지 못한다. 그래서 경계를 **드라이브 자체**로 긋는다. 사업 시트가 같이 있는 드라이브에 넣으면 그 계정이 시트까지 읽고 고칠 수
있으므로 반드시 전용으로 판다.

폼은 `FORM_FOLDER_ID` 가 가리키는 그 드라이브의 폴더 안에 만든다. 서비스 계정에는 개인
드라이브 저장용량이 없어 공유 드라이브 밖에서는 파일 생성 자체가 실패한다.

`api/google_sheets.py`가 읽기 전용인 것은 의도이므로 거기에 쓰기를 얹지 않는다. 계정이 곧
범위라는, 이 레포가 이미 쓰는 방식 그대로다.

### 6. 재사용은 문항을 건드리지 않는다

`create_form` 은 **만들기 도구다.** 같은 제목의 폼이 이미 있으면 문항을 그대로 두고 링크와
안내문만 다시 준다. 안내문 첫 줄도 "이미 있는 폼입니다"로 바꿔 사람이 새 폼으로 착각하지
않게 한다.

기존 스크립트의 재사용은 문항을 전부 지우고 다시 넣는 것인데 **그건 옮기지 않는다.**
문항을 갈아엎으면 `questionId` 가 새로 발급되고 사람이 이미 붙여 둔 응답 시트에 새 열이
생긴다. 기존 응답은 옛 열에 남아 어긋나고 시트만 봐서는 이유를 알 수 없다.

문항을 안 건드리면 "응답이 있는지"를 볼 필요가 없어진다. 그 판정은 `forms.responses.list`
가 필요하고 그 메서드는 `forms.body` 로는 안 돼서 스코프를 한 칸 더 열어야 한다.

동명이 둘 이상이면 formId 를 나열해 예외로 터뜨리고 사람이 고르게 한다. Drive 검색 색인은
즉시 일관적이지 않아 연달아 부르면 둘 다 0건을 보고 같은 제목 폼이 두 개 생길 수 있다.

## 레포 규칙에 맞출 것

- **도구는 `async def`.** `tests/test_tools_are_async.py`가 AST 로 훑어 강제한다.
- **구글 호출은 `asyncio.to_thread`로 넘긴다.** `create_form` 한 번에 왕복이 열 번 안팎이다.
  봇 넷과 스케줄러가 루프 하나를 공유하므로 그동안 전부 선다.
- **타임아웃은 호출마다 건다.** `googleapiclient`의 service 객체는 스레드 안전하지 않고
  `build()`는 `http`와 `credentials`를 함께 받지 않는다. service 는 계정별로 캐시하되
  (`api/google_sheets.py`의 관례) 호출은
  `execute(http=AuthorizedHttp(creds, http=httplib2.Http(timeout=30)))` 로 넘긴다. `httplib2`
  기본이 무한 대기라 이걸 빼면 워커를 무기한 붙잡는다. 재시도는 같은 이유로 붙이지 않는다.
- **`google-api-python-client`를 requirements.txt 에 핀해서 넣는다.** 지금 없다.
  `gspread~=6.2` 처럼 핀한다.
- **도구는 `app/general.py`의 반환 목록에 붙인다.** `tech.md`가 도구 8~9개면 성능이 떨어져
  셋씩 쪼갰다고 적었고 그 경로는 이미 그보다 많다. 하나 더 붙는 것이므로 답 품질을 같이 본다.

## 배치

```
api/google_forms.py       Forms·Drive REST 얇은 래퍼 (api/ 규칙: 래퍼만)
service/form.py           ①~⑦ 오케스트레이션 + 안내문
app/tools/form_tools.py   create_form (async @tool)
app/general.py            도구 목록에 추가
.env.example              FORM_SERVICE_ACCOUNT_JSON, FORM_FOLDER_ID, FORM_EDITORS
```

새 테이블도 엔드포인트도 스케줄도 없다. 설정은 환경변수로 둔다. `AppConfig`는 필드를 명시한
frozen dataclass라 `config.yaml`에 키를 하나 더하려면 `service/config.py`까지 고쳐야 하고
모르는 키는 조용히 버려져 그 사실이 예외로도 드러나지 않는다.

제목으로 기존 폼을 찾을 때 `files.list` 함정이 넷이다. `trashed=false`, 공유 드라이브 두
플래그, 그리고 **제목 이스케이프**다. 앞 셋은 `api/google_sheets.py`의
`list_spreadsheet_files`에 적혀 있다. 넷째는 제목이 슬랙 문장에서 오기 때문에 새로 생긴다.

```python
def _quote(title: str) -> str:
    """Drive 쿼리 리터럴. `'짓다' 신청` 같은 제목이 400 으로 죽지 않게."""
    return title.replace("\\", "\\\\").replace("'", "\\'")
```

이스케이프를 빼면 따옴표가 든 제목에서 죽고 조작된 제목은 쿼리 조건을 바꿔 무관한 파일이
재사용 대상으로 잡힌다. 그 파일에 `anyone` 공개와 편집자 공유가 그대로 실행된다.

문항 스펙은 `repo/industry-linked/create_*_form.py`의 `QUESTIONS` 튜플을 그대로 옮긴다.

```python
(title, description, kind, required, options)
# kind: short | para | radio | radio_other | check | scale
# 파일 업로드는 없다. Forms API 가 생성을 지원하지 않고 폼 UI 에도 안 나온다(8/4 실측).
# 사본이 필요하면 메일로 받는다 — staff_doc 이 그렇게 받고 있다.
```

일곱 스크립트가 실제로 쓴 유형이 이 여섯이다. `radio_other`는 선택지 끝에 `{"isOther": True}`
를 붙여 '기타' 직접 입력을 여는 것으로, 빠뜨리면 선택지로 다 못 덮는 문항이 막힌다.
`dropdown`·`date`는 한 번도 안 썼으므로 넣지 않는다.

## 선행 작업 (사람이 콘솔에서)

| # | 할 일 | 어디서 |
|---|---|---|
| 1 | 서비스 계정 만들고 JSON 키 발급 | [IAM 서비스 계정](https://console.cloud.google.com/iam-admin/serviceaccounts?project=elegant-circle-503206-a1) |
| 2 | Forms API 사용 설정 | [Forms API](https://console.cloud.google.com/apis/library/forms.googleapis.com?project=elegant-circle-503206-a1) |
| 3 | Drive API 사용 설정 | [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com?project=elegant-circle-503206-a1) |
| 4 | **폼 전용 공유 드라이브**를 새로 만들고 1번 계정을 콘텐츠 관리자로 추가 | [공유 드라이브](https://drive.google.com/drive/shared-drives) |
| 5 | 그 드라이브에서 **외부 공유(링크가 있는 모든 사용자)가 허용**돼 있는지 확인 | 드라이브 설정 |
| 6 | `tmn-secret-prd`에 `workflow_form_service_account_json` 추가 | [Secrets Manager](https://ap-northeast-2.console.aws.amazon.com/secretsmanager/listsecrets?region=ap-northeast-2) |
| 7 | 헬름에 `FORM_SERVICE_ACCOUNT_JSON`(시크릿) + `FORM_FOLDER_ID`·`FORM_EDITORS`(env) 추가 | 레포 PR |

4번이 빠지면 `files.create`가 권한 없음으로 죽는다. 5번이 빠지면 `permissions.create` 의
`anyone` 이 403 이고 원인이 코드에 없어 찾는 데 오래 걸린다.

## 안 하는 것

- **응답 동기화.** 사람이 한 번 누르면 구글이 실시간으로 붙여 준다. 우리가 퍼 나르면 지연에
  더해 스케줄·쿼터·헤더 변경까지 떠안는다.
- **자체 단축 링크.** 사람이 진짜 `forms.gle`을 받는다.
- **`list_forms` tool.** 만든 직후 안내로 끝난다. 목록이 필요해지면 그때 붙인다.
- **승인 게이트.** 문자와 달리 폼은 만든 뒤 고칠 수 있다.
- **폼 삭제·응답 수정 도구.**
