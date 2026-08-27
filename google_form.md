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

### 1. 공개는 생성의 일부다

`create_form()`이 문항·공개·게시·편집자 공유·되읽기 검증을 마친 뒤에야 링크를 돌려준다.
실패하면 예외를 던지며 인자로 끄고 켜지 못한다.

**편집자 공유도 검증에 넣는다.** 이것만 실패하면 폼은 만들어졌는데 사람이 ①②를 누를 수
없고 그 상태로 "만들었습니다" 안내가 나간다.

```python
perms = drive.permissions().list(
    fileId=fid, fields="permissions(type,role,emailAddress)",
    supportsAllDrives=True).execute()["permissions"]
assert any(p["type"] == "anyone" and p["role"] == "reader" for p in perms), "링크 공개가 안 됐다"
shared = {p.get("emailAddress") for p in perms if p["role"] == "writer"}
assert shared >= set(editors), f"편집자 공유 실패: {set(editors) - shared}"
state = svc.forms().get(formId=fid).execute().get("publishSettings", {}).get("publishState", {})
assert state.get("isPublished") and state.get("isAcceptingResponses"), "게시가 안 됐다"
```

`.get`을 겹쳐 쓰는 이유는 `publishSettings`가 없을 때 `KeyError`가 나면 위 한국어 실패
메시지가 안 나오기 때문이다. 기존 스크립트도 이렇게 쓴다.

`create_jitda_event_form`의 공유·이동은 `except Exception: pass`인데 **그건 옮기지 않는다.**
AGENTS.md 가 예외를 삼키지 말라고 못 박았고 삼키면 지금 검증하려는 실패가 정확히 숨는다.

### 2. 남은 두 번은 링크로 넘긴다

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
막히는데, 목록이 보이면 그 자리에서 안다.

### 3. 편집자는 사내 주소만 받는다

편집자 인자는 LLM 이 채운다. 제한이 없으면 슬랙에서 "이 주소도 편집자로 넣어줘" 한 마디로
아무 주소나 writer 가 된다. 폼 writer 는 **응답 전량**을 본다. 성함·소속 학교·연락처다.
게다가 기존 스크립트처럼 `sendNotificationEmail=False`로 붙이면 붙은 당사자에게도 메일이
가지 않아 슬랙 스레드 밖에서는 드러나지 않는다.

인자로 받는 주소는 `@team-mono.com`만 통과시킨다. 외부 편집자가 필요하면 그때 사람이
`FORM_EDITORS`에 적는다.

### 4. 계정과 폴더

`FORM_SERVICE_ACCOUNT_JSON`을 새로 판다. 스코프는 **`drive.file`과 `forms.body`** 다.

`drive.file`은 그 앱이 만든 파일만 열어 준다. 전체 `drive`를 주면 그 계정에 지금 또는
앞으로 공유되는 모든 것이 열리므로, "자기가 만든 폼뿐"이라는 경계가 스코프로 지켜지지
않는다. **다만 공유 드라이브 폴더를 `parents`로 지정한 생성이 `drive.file`로도 되는지는
새 계정으로 한 번 찍어 확정할 것.** 안 되면 그때만 `drive`로 올린다.

폼은 `FORM_FOLDER_ID`가 가리키는 **공유 드라이브 폴더 안에** 만든다. 서비스 계정에는 개인
드라이브 저장용량이 없어 그 밖에서는 파일 생성 자체가 실패한다. 새로 판 계정은 그 공유
드라이브의 멤버가 아니므로 **콘텐츠 관리자로 넣는 것이 선행 작업이다.** 기존 스크립트가
되는 이유는 그 SA 가 이미 멤버라서다.

`api/google_sheets.py`가 읽기 전용인 것은 의도이므로 거기에 쓰기를 얹지 않는다. 계정이 곧
범위라는, 이 레포가 이미 쓰는 방식 그대로다.

### 5. 재사용은 문항을 덮어쓰지 않는다

같은 제목의 폼이 있으면 재사용한다. 슬랙에서 두 번 부르면 폼이 두 개가 되기 때문이다.

**다만 기존 스크립트의 재사용은 문항을 전부 지우고 다시 넣는 것이라 그대로 옮기면 안 된다.**
문항을 갈아엎으면 `questionId`가 새로 발급되고 사람이 이미 붙여 둔 응답 시트에는 새 열이
생긴다. 기존 응답은 옛 열에 남아 어긋나고 시트만 봐서는 이유를 알 수 없다.

응답이 있거나 `linkedSheetId`가 찬 폼은 문항 덮어쓰기를 거부하고 링크와 안내문만 다시 준다.

### 6. 파일 업로드 문항은 막는다

파일 업로드가 있으면 응답자가 구글 로그인을 해야 해서 링크 공개와 양립하지 않는다. 빌더에서
거부하고 사본은 메일로 받으라고 안내한다. `staff_doc` 이 이미 그렇게 받고 있다.

## 레포 규칙에 맞출 것

- **도구는 `async def`.** `tests/test_tools_are_async.py`가 AST 로 훑어 강제한다.
- **구글 호출은 `asyncio.to_thread`로 넘긴다.** `create_form` 한 번에 왕복이 열 번 안팎이다.
  봇 넷과 스케줄러가 루프 하나를 공유하므로 그동안 전부 선다.
- **타임아웃을 건다.** `googleapiclient`는 `httplib2`를 쓰고 기본이 무한 대기다. 재시도를
  안 붙이는 이유가 워커를 안 붙잡으려는 것인데 타임아웃이 없으면 같은 일이 난다.
  `api/google_sheets.py`의 `TIMEOUT_SECONDS`(30초)에 맞춘다.
- **`google-api-python-client`를 requirements.txt 에 핀해서 넣는다.** 지금 없다.
  `gspread~=6.2`처럼 무핀으로 두지 않는다.

## 배치

```
api/google_forms.py       Forms·Drive REST 얇은 래퍼 (api/ 규칙: 래퍼만)
service/form.py           생성 → 문항 → 공개 → 편집자 → 검증 → 안내문
app/tools/form_tools.py   create_form (async @tool)
.env.example              FORM_SERVICE_ACCOUNT_JSON, FORM_FOLDER_ID, FORM_EDITORS
```

새 테이블도 엔드포인트도 스케줄도 없다. 설정은 환경변수로 둔다. `AppConfig`는 필드를 명시한 frozen dataclass라 `config.yaml`에
키를 하나 더하려면 `service/config.py`까지 고쳐야 하고 모르는 키는 조용히 버려져 그
사실이 예외로도 드러나지 않는다.

제목으로 기존 폼을 찾을 때는 `files.list`에 `trashed=false`와 공유 드라이브 두 플래그가
필요하다. 함정 셋 다 `api/google_sheets.py`의 `list_spreadsheet_files`에 적혀 있다. 제목
정확 일치로 찾으므로 페이지네이션은 필요 없다.

문항 스펙은 `repo/industry-linked/create_*_form.py`의 `QUESTIONS` 튜플을 그대로 옮긴다.

```python
(title, description, kind, required, options)
# kind: short | para | radio | radio_other | check | scale
```

일곱 스크립트가 실제로 쓴 유형이 이 여섯이다. `radio_other`는 선택지 끝에 `{"isOther": True}`를
붙여 '기타' 직접 입력을 여는 것으로, 빠뜨리면 선택지로 다 못 덮는 문항이 막힌다.
`dropdown`·`date`는 한 번도 안 썼으므로 넣지 않는다.

## 선행 작업 (사람이 콘솔에서)

| # | 할 일 | 어디서 |
|---|---|---|
| 1 | 서비스 계정 만들고 JSON 키 발급 | [IAM 서비스 계정](https://console.cloud.google.com/iam-admin/serviceaccounts?project=elegant-circle-503206-a1) |
| 2 | Forms API 사용 설정 | [Forms API](https://console.cloud.google.com/apis/library/forms.googleapis.com?project=elegant-circle-503206-a1) |
| 3 | Drive API 사용 설정 | [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com?project=elegant-circle-503206-a1) |
| 4 | 폼을 담을 **공유 드라이브** 폴더를 정하고 1번 계정을 콘텐츠 관리자로 추가 | [공유 드라이브](https://drive.google.com/drive/shared-drives) |
| 5 | `tmn-secret-prd`에 `workflow_form_service_account_json` 추가 | [Secrets Manager](https://ap-northeast-2.console.aws.amazon.com/secretsmanager/listsecrets?region=ap-northeast-2) |
| 6 | `jce-service-helm`에 `FORM_SERVICE_ACCOUNT_JSON` 매핑 추가 | 레포 PR |

4번이 빠지면 `files.create`가 권한 없음으로 죽는다.

## 안 하는 것

- **응답 동기화.** 사람이 한 번 누르면 구글이 실시간으로 붙여 준다. 우리가 퍼 나르면 지연에
  더해 스케줄·쿼터·헤더 변경까지 떠안는다.
- **자체 단축 링크.** 사람이 진짜 `forms.gle`을 받는다.
- **`list_forms` tool.** 만든 직후 안내로 끝난다. 목록이 필요해지면 그때 붙인다.
- **승인 게이트.** 문자와 달리 폼은 만든 뒤 고칠 수 있다.
- **폼 삭제·응답 수정 도구.**
