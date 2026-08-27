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
| 응답자 공개 + 게시 | 봇 | 빠뜨리면 응답이 0건이다 |
| 편집자 공유 | 봇 | 없으면 사람이 아래 둘을 누를 수 없다 |
| **응답 시트 연결** | **사람** | API 에 스위치가 없다. 폼 화면에서 누르면 실시간으로 붙는다 |
| **`forms.gle` 단축 링크** | **사람** | API 에 없다. 폼 화면 '보내기'에서 받는다 |

아래 둘을 흉내 내지 않는다. 우리가 응답을 퍼 나르면 지연이 생기고 우리 도메인으로 단축을
발급하면 엔드포인트와 표가 는다. 사람이 누르면 구글이 진짜로 붙여 주고 진짜 `forms.gle`을 준다.

## 공개는 두 겹이고, 이제 빠뜨리면 무조건 터진다

응답자 공개와 게시는 별개다. 하나만 해도 응답이 0건이다.

**2026-07-01 부터 API 로 만든 폼은 기본이 미게시다.** 그 전에는 하위 호환으로 게시된 채
만들어졌다. 오늘(8/27)은 이미 지난 뒤라 게시를 빼먹은 폼은 예외 없이 응답을 못 받는다.
`staff_doc`·`short_term` 방식은 지금 그대로 돌리면 죽는다.

**응답자 공개는 `view: "published"` 로 준다.**

```python
drive.permissions().create(fileId=fid, supportsAllDrives=True,
    body={"type": "anyone", "role": "reader", "view": "published"}).execute()
```

`view` 없이 주는 `anyone`/`reader` 로도 응답은 받아진다. 일곱 스크립트가 그렇게 하고 있다.
다만 그것은 **파일 자체**를 링크 공개로 여는 것이고 `view: "published"` 는 게시된 응답
화면만 연다. 계정 범위를 좁혀 놓고 파일을 필요보다 넓게 열 이유가 없다.

되읽을 때는 `permissions.list` 에 **`includePermissionsForView="published"`** 를 줘야
이 권한이 응답에 들어온다. 안 주면 사람이 폼 UI 로 연 응답자 권한도 영영 못 본다.

## 설계

### 1. 순서가 곧 안전장치다

```
① files.create        전용 공유 드라이브 폴더에 폼 셸을 만든다
② deleteItem × N      셸에 딸려 온 기본 문항을 역순으로 지운다
③ batchUpdate         제목·설명·문항을 넣는다
④ 편집자 공유         → 그 자리에서 되읽어 확인
⑤ setPublishSettings  → 그 자리에서 되읽어 확인
⑥ 응답자 공개         → 그 자리에서 되읽어 확인
```

**②를 빼먹으면 만드는 폼마다 1번 문항이 "제목 없는 질문"이다.** Drive 우회로 만든 셸에는
기본 문항이 딸려 온다. `deleteItem`이 뒤 인덱스를 당기므로 **역순으로** 지운다. 정순으로
지우면 절반이 남는다.

**응답자 공개가 맨 뒤다.** 중간에 죽으면 남는 폼을 아무도 못 연다. 폼 삭제 도구는 만들지
않으므로 공개된 채 남으면 치울 방법이 없다.

**검증을 뒤에 몰지 않는다.** 단계마다 그 자리에서 되읽는다. ⑥ 뒤에 한 번에 확인하면 편집자
공유가 실패했을 때 이미 공개된 폼이 남는다. 순서만 바꾸고 검증을 몰면 위험 구간이 좁아질 뿐
없어지지 않는다.

### 2. 실패는 예외가 아니라 formId 를 담은 문자열로 돌려준다

이 도구는 `create_react_agent` 아래에서 돈다. LangGraph `ToolNode` 는 예외를 잡아
"Error: … Please fix your mistakes." 로 감싸 모델에게 되돌리고 모델은 같은 도구를 다시
부른다. **①이 이미 끝난 뒤라면 재호출은 폼을 하나 더 만든다.**

그래서 ① 이후의 실패는 예외로 올리지 않고 문자열로 내린다. `app/tools/redash_tools.py` 가
이미 쓰는 관례다.

```
폼은 만들어졌지만 ⑤ 게시에서 멈췄습니다.
  formId : 1AbC...
  편집   : https://docs.google.com/forms/d/1AbC.../edit
  이어서 하려면 form_id 를 넘겨 다시 불러 주세요.
```

`create_form(..., form_id: str | None = None)` 으로 이어받는다. `form_id` 가 오면 ①②③ 을
건너뛰고 ④부터 다시 돈다. `service/form.py` 안에서는 예외를 던지고 tool 경계에서 잡아
문자열로 바꾼다. 예외 메시지에도 formId 와 편집 링크를 싣는다.

`assert` 는 쓰지 않는다. `api/google_sheets.py` 처럼 사람이 다음에 뭘 할지 담아
`ValueError` 를 던진다.

### 3. 편집자 검증은 부여와 같은 기준으로 본다

```python
# 공유 드라이브 멤버십은 파일 권한에 organizer·fileOrganizer 로 상속돼 내려온다.
# writer 만 세면 그 폴더를 관리하는 사람이 "공유 실패"로 잡힌다.
WRITE_ROLES = {"writer", "fileOrganizer", "organizer"}

def _permissions(drive, fid: str) -> list[dict]:
    """페이지를 끝까지 돈다. 공유 드라이브는 pageSize 최댓값이 100 이고
    nextPageToken 을 fields 에 안 넣으면 잘렸는지조차 알 수 없다."""
    params = {
        "fileId": fid, "supportsAllDrives": True, "pageSize": 100,
        "includePermissionsForView": "published",
        "fields": "nextPageToken,permissions(id,type,role,view,emailAddress)",
    }
    ...
```

**부여를 건너뛰는 기준과 통과 판정 기준이 같아야 한다.** 편집자가 이미 `reader` 나
`commenter` 로 붙어 있으면, "있는 사람은 건너뛴다"로 짜면 부여를 건너뛰는데,
판정은 `WRITE_ROLES` 만 인정하므로 그 사람이 실패로 잡힌다. 건너뛰기는 `WRITE_ROLES` 인 사람만 하고, 낮은 역할이 이미
있으면 `permissions.create` 가 아니라 그 permission id 로 `permissions.update` 를 부른다.
공유 드라이브는 상속된 역할보다 낮은 파일 단위 부여를 거부하므로 이미 콘텐츠 관리자인
사람에게 `writer` 를 주려 들면 그 호출 자체가 실패한다.

`emailAddress` 는 `type` 이 `user`·`group` 일 때만 온다. 편집자가 그룹으로 권한을 받고
있으면 개인 주소는 응답 어디에도 없다. **그런 주소는 실패로 치지 않고 안내문에 "확인 불가"로
적는다.** 판정은 ④ 에서 방금 부여했거나 `WRITE_ROLES` 로 직접 확인한 주소로 한정한다.

`create_jitda_event_form`의 공유·이동은 `except Exception: pass`인데 **그건 옮기지 않는다.**
AGENTS.md 가 예외를 삼키지 말라고 못 박았고 삼키면 지금 검증하려는 실패가 정확히 숨는다.

### 4. 남은 두 번은 링크로 넘긴다

무엇을 눌러야 하는지까지 답에 박아 준다. 응답 링크는 `forms.get`이 주는 `responderUri`를
그대로 쓴다. **그 URL 의 ID 는 `formId`와 다른 값이라 조합해서 만들 수 없다.**

```
폼을 만들었습니다. 링크를 뿌리기 전에 두 가지만 눌러 주세요.

  ① 응답 시트 연결  https://docs.google.com/forms/d/{formId}/edit#responses
     '응답' 탭 → 시트 아이콘 → [새 스프레드시트 만들기]

  ② 짧은 주소 받기  https://docs.google.com/forms/d/{formId}/edit
     오른쪽 위 [보내기] → 링크 아이콘 → 'URL 단축' 체크 → 복사

  공개 상태 : 응답자 공개 O · 게시 O
  편집자   : byb@team-mono.com, chk@team-mono.com
  응답 링크 : {responderUri}
```

②가 필요한 이유는 길이다. 원본 응답 링크는 100자라 EUC-KR 90바이트인 문자 한 통에 안
들어가서 단축 없이 보내면 LMS 로 넘어가 통당 단가가 오른다.

**편집자 목록을 안내문에 같이 찍는다.** 부른 사람이 그 목록에 없으면 ①②가 권한 없음으로
막히는데 목록이 보이면 그 자리에서 안다.

### 5. 편집자는 사내 주소만, 최소 한 명

편집자 인자는 LLM 이 채운다. 제한이 없으면 슬랙에서 "이 주소도 편집자로 넣어줘" 한 마디로
아무 주소나 writer 가 된다. 폼 writer 는 **응답 전량**을 본다. 성함·소속 학교·연락처다.
게다가 기존 스크립트처럼 `sendNotificationEmail=False`로 붙이면 붙은 당사자에게도 메일이
가지 않아 슬랙 스레드 밖에서는 드러나지 않는다.

- 주소는 소문자로 바꾼 뒤 **`@team-mono.com` 접미사**로 판정한다. `endswith("team-mono.com")`
  으로 짜면 `evil-team-mono.com` 이 통과한다.
- **편집자가 한 명도 없으면 거부한다.** 판정 집합은 목록이 비면 항상 통과라 아무도 못 여는
  폼이 "검증 통과"로 나간다.
- 기본값은 `FORM_EDITORS`(쉼표 구분)에서 읽는다.

### 6. 폼 설명은 plain text 다

폼 description 에 마크다운을 넣으면 `**` 가 그대로 노출된다. 기존 스크립트는
`assert "**" not in DESCRIPTION` 으로 이걸 막는다.

**이 설계에서는 그 글을 LLM 이 쓴다.** 사람이 손으로 쓰던 때보다 마커가 섞일 확률이 훨씬
높다. 폼 제목·설명과 문항 제목·설명에서 마크다운 마커를 검사해 거부한다. 선생님이 보는
화면에 `**` 가 찍히는 것은 되돌릴 수 없다.

### 7. 계정과 폴더

`FORM_SERVICE_ACCOUNT_JSON`을 새로 판다. 스코프는 `drive`·`forms.body` 다.

`drive.file` 로 좁히면 경계가 스코프로 지켜지지만 그 스코프는 앱이 만든 파일만 열기 때문에
앱 바깥에서 만든 폴더를 `parents` 로 주는 생성이 통과하지 못한다.

**그래서 폼 전용 공유 드라이브를 따로 파고 이 계정을 거기에만 넣는다.** full `drive` 는 그
계정이 볼 수 있는 모든 파일을 읽고 고치게 하고 `forms.responses.list` 까지 열어 **그
드라이브 안 모든 폼의 응답을 읽게 한다.** §5 가 편집자를 제한하는 근거가 "writer 는 응답
전량을 본다"인데 이 계정 자신이 그렇다. 사업 시트가 같이 있는 드라이브에 넣으면 안 된다.

폼은 `FORM_FOLDER_ID` 가 가리키는 그 드라이브의 폴더 안에 만든다. 서비스 계정에는 개인
드라이브 저장용량이 없어 공유 드라이브 밖에서는 파일 생성 자체가 실패한다.

`api/google_sheets.py`가 읽기 전용인 것은 의도이므로 거기에 쓰기를 얹지 않는다. 계정이 곧
범위라는, 이 레포가 이미 쓰는 방식 그대로다.

## 레포 규칙에 맞출 것

- **도구는 `async def`.** `tests/test_tools_are_async.py`가 AST 로 훑어 강제한다.
- **구글 호출은 `asyncio.to_thread`로 넘긴다.** `create_form` 한 번에 왕복이 열 번 안팎이다.
  봇 넷과 스케줄러가 루프 하나를 공유하므로 그동안 전부 선다.
- **타임아웃을 걸되 커넥션은 재사용한다.** `googleapiclient` 의 service 객체는 스레드
  안전하지 않고 `build()` 는 `http` 와 `credentials` 를 함께 받지 않는다. service 는 캐시하고
  (`api/google_sheets.py` 의 관례) 호출은 `execute(http=AuthorizedHttp(creds, http=_http()))`
  로 넘긴다. `_http()` 는 `httplib2.Http(timeout=30)` 를 **스레드로컬로 재사용**한다.
  호출마다 새로 만들면 왕복 열 번이 TLS 핸드셰이크 열 번이 된다. `httplib2` 기본이 무한
  대기라 타임아웃은 반드시 건다. 재시도는 붙이지 않는다.
- **requirements.txt 에 셋을 핀해서 넣는다.** `google-api-python-client`,
  `google-auth-httplib2`, `httplib2` 다. 뒤 둘은 코드가 직접 import 하는데 지금은 전이
  의존으로 딸려 올 뿐이다. `gspread~=6.2` 위에 적힌 이유가 그대로 적용된다.
- **도구는 `app/general.py`의 반환 목록에 붙인다.** `tech.md`가 도구 8~9개면 성능이 떨어져
  셋씩 쪼갰다고 적었고 그 경로는 이미 그보다 많다. 하나 더 붙는 것이므로 답 품질을 같이 본다.

## 배치

```
api/google_forms.py         Forms·Drive REST 얇은 래퍼 (api/ 규칙: 래퍼만)
service/form.py             ①~⑥ 오케스트레이션 + 안내문
app/tools/form_tools.py     create_form (async @tool)
app/general.py              도구 목록에 추가
tests/test_form_requests.py 문항 스펙 → createItem 변환, 마크다운 거부
.env.example                FORM_SERVICE_ACCOUNT_JSON, FORM_FOLDER_ID, FORM_EDITORS
```

새 테이블도 엔드포인트도 스케줄도 없다. 설정은 환경변수로 둔다. `AppConfig`는 필드를 명시한
frozen dataclass라 `config.yaml`에 키를 하나 더하려면 `service/config.py`까지 고쳐야 하고
모르는 키는 조용히 버려져 그 사실이 예외로도 드러나지 않는다.

테스트는 API 없이 도는 두 가지만 본다. 문항 스펙을 `createItem` 요청으로 옮기는 변환과
마크다운 거부다. 나머지는 구글이 답해야 알 수 있다.

### 문항 스펙

위치 튜플을 쓰지 않는다. `options` 가 유형에 따라 "선택지 목록"과 "양끝 라벨 쌍" 두 가지를
뜻하게 되는데(`create_survey_form.py` 가 그렇다) 그 인자를 LLM 이 채우면 `scale` 에서
`scaleQuestion` 의 필수 필드를 못 채워 400 이 난다.

```python
@dataclass
class Question:
    title: str
    kind: Literal["short", "para", "radio", "radio_other", "check", "scale"]
    description: str = ""
    required: bool = True
    options: list[str] | None = None      # radio · radio_other · check
    low_label: str | None = None          # scale
    high_label: str | None = None         # scale
```

`scale` 은 `low=1`·`high=5` 로 고정한다. Forms 는 `low` 를 0 또는 1, `high` 를 3~10 만
받으므로 LLM 이 임의 값을 넣으면 400 이다. 5점 리커트가 사업계획서가 약속한 측정 방식이기도
하다.

일곱 스크립트가 실제로 쓴 유형이 이 여섯이다. `radio_other`는 선택지 끝에 `{"isOther": True}`
를 붙여 '기타' 직접 입력을 여는 것으로 빠뜨리면 선택지로 다 못 덮는 문항이 막힌다.
`dropdown`·`date`는 한 번도 안 썼으므로 넣지 않는다. **파일 업로드는 넣을 수 없다.**
Forms API 가 생성을 지원하지 않고 폼 UI 에도 안 나온다(8/4 실측). 사본이 필요하면 메일로
받는다. `staff_doc` 이 그렇게 받고 있다.

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

4번이 빠지면 `files.create`가 권한 없음으로 죽는다. 5번이 빠지면 응답자 공개가 403 이고
원인이 코드에 없어 찾는 데 오래 걸린다.

## 안 하는 것

- **응답 동기화.** 사람이 한 번 누르면 구글이 실시간으로 붙여 준다. 우리가 퍼 나르면 지연에
  더해 스케줄·쿼터·헤더 변경까지 떠안는다.
- **자체 단축 링크.** 사람이 진짜 `forms.gle`을 받는다.
- **제목으로 기존 폼 재사용.** 조회 하나에 딸려 오는 것이 너무 많다. Drive 쿼리 이스케이프,
  폴더 한정, 색인 지연으로 인한 중복, 사람이 일부러 닫아 둔 폼을 다시 여는 문제까지다.
  중복 생성은 실패를 `form_id` 로 이어받게 해서 막는다. 같은 제목으로 두 번 부르면 폼이 둘
  생기는데 그건 사람이 드라이브에서 지운다.
- **`list_forms` tool.** 만든 직후 안내로 끝난다.
- **승인 게이트.** 문자와 달리 폼은 만든 뒤 고칠 수 있다.
- **폼 삭제·응답 수정 도구.**
