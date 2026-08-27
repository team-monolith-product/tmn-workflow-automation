# 구글폼 생성

선생님에게 정보를 받는 가장 쉬운 통로가 구글폼이다. 슬랙에서 "이런 폼 만들어줘"라고 말하면
봇이 폼을 만들어 공개하고 사람이 마지막 두 번을 클릭할 링크를 같이 준다.

지금은 이 일이 `industry-linked/create_*_form.py` 일곱 개에 복붙으로 흩어져 있다. 그중
**공개 처리를 둘 다 갖춘 것은 넷뿐이다.**

| 스크립트 | 링크 공개 | 게시 |
|---|---|---|
| create_demand_form / jitda_event / team / visit | O | O |
| create_staff_doc_form | O | **X** |
| create_survey_form | **X** | O |
| create_short_term_form | **X** | **X** |

사람이 매번 옮겨 적으니 매번 다르게 빠진다. 공개를 부르는 쪽의 선택지로 두는 한 이 표는 계속 나온다.

## 봇이 하는 것과 사람이 하는 것

| | 누가 | 왜 |
|---|---|---|
| 폼 만들기 | 봇 | `forms.create`는 서비스 계정에서 500이라 Drive로 우회한다 |
| 문항 채우기 | 봇 | `batchUpdate` |
| 링크 공개 + 게시 | 봇 | **빠뜨리면 응답이 0건이다.** 아래 참조 |
| 편집자 공유 | 봇 | 사람이 아래 두 가지를 하려면 편집 권한이 있어야 한다 |
| **응답 시트 연결** | **사람** | API에 스위치가 없다. 폼 화면에서 한 번 누르면 실시간으로 붙는다 |
| **`forms.gle` 단축 링크** | **사람** | API에 없다. 폼 화면 '보내기'에서 받는다 |

시트 연결과 단축 링크를 API로 흉내 내지 않는다. 응답을 우리가 퍼 나르면 지연이 생기고
우리 도메인으로 단축을 발급하면 서버와 표가 하나씩 는다. 사람이 폼 화면에서 누르면 구글이
진짜로 붙여 주고 진짜 `forms.gle`을 준다. 봇은 **거기까지 가는 링크를 정확히 주는 일**만 한다.

## 공개는 두 겹이고, 이제 안 하면 무조건 터진다

링크 공개(Drive `permissions.create` `anyone`/`reader`)와 게시(Forms `setPublishSettings`
`isPublished`+`isAcceptingResponses`)는 별개다. 하나만 해도 응답이 0건이다.

**2026년 7월 1일부터 API로 만든 폼은 기본이 미게시다.** 그 전에는 하위 호환으로 게시된 채
만들어졌다. 오늘(8/27)은 이미 지난 뒤라, 게시를 빼먹은 폼은 예외 없이 응답을 못 받는다.
`create_staff_doc_form`·`create_short_term_form` 방식은 지금 그대로 돌리면 죽는다.

## 설계

### 1. 공개는 생성의 일부다

`create_form()`이 문항·링크 공개·게시·편집자 공유·되읽기 검증까지 마친 뒤에야 링크를
돌려준다. 공개에 실패하면 폼을 돌려주지 않고 예외를 던지며 인자로 끄고 켜지 못한다.

되읽기는 `create_jitda_event_form`이 하던 그대로다.

```python
perms = drive.permissions().list(fileId=fid, fields="permissions(type,role)",
                                 supportsAllDrives=True).execute()["permissions"]
assert any(p["type"] == "anyone" for p in perms), "링크 공개가 안 됐다"
state = svc.forms().get(formId=fid).execute()["publishSettings"]["publishState"]
assert state.get("isPublished") and state.get("isAcceptingResponses"), "게시가 안 됐다"
```

만드는 자리에서 확인하므로, 링크를 카톡에 뿌리기 전 슬랙 대화 안에서 문제가 드러난다.

### 2. 남은 두 번은 링크로 넘긴다

봇의 답에 사람이 누를 자리를 박아 준다. 무엇을 눌러야 하는지까지 적는다.

```
폼을 만들었습니다. 링크를 뿌리기 전에 두 가지만 눌러 주세요.

  ① 응답 시트 연결
     https://docs.google.com/forms/d/{formId}/edit#responses
     '응답' 탭 → 시트 아이콘 → [새 스프레드시트 만들기]

  ② 짧은 주소 받기
     https://docs.google.com/forms/d/{formId}/edit
     오른쪽 위 [보내기] → 링크 아이콘 → 'URL 단축' 체크 → 복사

  공개 상태 : 링크 공개 O · 게시 O
  응답 링크 : https://docs.google.com/forms/d/e/{responderId}/viewform
```

②가 필요한 이유는 길이다. 원본 응답 링크는 100자라 EUC-KR 90바이트인 문자 한 통에 아예 안
들어간다. 단축을 안 받고 문자로 보내면 LMS로 넘어가 통당 단가가 오른다.

**①은 봇이 나중에 확인할 수 있다.** `linkedSheetId`는 쓰지는 못해도 읽을 수는 있는 필드라,
`list_forms`가 이 값이 빈 폼을 "시트 미연결"로 표시한다. 사람이 깜빡한 것을 봇이 짚어 준다.
②는 확인할 방법이 없으므로 안내에서 끝낸다.

### 3. 편집자 공유는 선택이 아니다

①과 ②를 누르려면 그 사람에게 폼 편집 권한이 있어야 한다. 봇이 만든 폼은 서비스 계정 소유라
공유하지 않으면 아무도 못 연다. 그러니 생성 단계에서 편집자를 붙인다.

기본 편집자 목록은 `config.yaml`에 두고 tool 인자로 더한다. 슬랙에서 부른 사람의 메일을
자동으로 넣으려면 `users:read.email` 스코프가 필요한데, 그건 나중에 필요해지면 붙인다.

### 4. 계정이 권한 경계다

`FORM_SERVICE_ACCOUNT_JSON`을 새로 판다. 기존 둘은 읽기 전용으로 그대로 둔다.

| 계정 | 스코프 | 보는 범위 |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 읽기 | 학교 일정 시트 |
| `OPERATING_SHEET_SERVICE_ACCOUNT_JSON` | 읽기 | 사업 운영 시트 |
| `FORM_SERVICE_ACCOUNT_JSON` | `drive`, `forms.body` | **자기가 만든 폼뿐** |

시트를 사람이 붙이므로 이 계정에 `spreadsheets` 스코프는 필요 없다. 응답 시트는 구글이
만들어 폼 소유자 아래 두고 사람은 편집자로 그것을 연다.

`api/google_sheets.py`가 읽기 전용인 것은 의도다. 거기에 쓰기를 얹으면 봇이 사람이 관리하는
시트를 고칠 수 있게 된다. 새 계정은 쓰기를 갖지만 사업 시트 어디에도 공유돼 있지 않으므로
건드릴 것이 없다. 계정이 곧 범위라는, 이 레포가 이미 쓰는 방식 그대로다.

폼은 **공유 드라이브 폴더 안에** 만든다. 서비스 계정에는 개인 드라이브 저장용량이 없어서
그 밖에서는 파일 생성 자체가 실패한다.

### 5. 파일 업로드 문항은 막는다

파일 업로드 문항이 있는 폼은 응답자가 구글 로그인을 해야 한다. 링크 공개와 양립하지 않는다.
빌더에서 거부하고 사본은 이메일로 받으라고 안내한다. `create_staff_doc_form`이 신분증·통장
사본을 이미 그렇게 받고 있다.

## 배치

```
api/google_forms.py       Forms·Drive REST 얇은 래퍼 (api/ 규칙: 래퍼만)
service/form.py           생성 → 문항 → 공개 → 편집자 → 검증 → 안내문
app/tools/form_tools.py   create_form, list_forms (LangChain tool)
```

새 테이블도, 새 엔드포인트도, 새 스케줄도 없다. 폼 목록은 Drive `files.list`로
`mimeType='application/vnd.google-apps.form'`을 훑으면 나온다. `api/google_sheets.py`의
`list_spreadsheet_files`와 같은 방식이고 거기 적힌 두 함정이 그대로 적용된다. 휴지통을
빼려면 `trashed=false`가 필요하고, 공유 드라이브를 보려면 `supportsAllDrives`와
`includeItemsFromAllDrives`가 필요하다.

문항 스펙은 기존 스크립트의 `QUESTIONS` 튜플을 그대로 스키마로 옮긴다.

```python
(title, description, kind, required, options)
# kind: short | para | radio | check | dropdown | scale | date  (file 없음)
```

같은 제목의 폼이 있으면 재사용한다. 기존 스크립트 전부가 하던 것이고 슬랙에서 두 번 부르면
폼이 두 개가 되는 사고를 막는다.

## 안 하는 것

- **응답을 시트로 퍼 나르는 동기화.** 사람이 한 번 누르면 구글이 실시간으로 붙여 준다.
  우리가 퍼 나르면 지연이 생기고 스케줄·쿼터·헤더 변경을 떠안는다.
- **자체 단축 링크 발급.** 사람이 폼 화면에서 진짜 `forms.gle`을 받는다. 리다이렉트
  엔드포인트와 슬러그 표를 만들 이유가 없다.
- **승인 게이트.** 문자와 달리 폼은 만든 뒤 고칠 수 있다. 오타 하나에 카드를 한 장 더 띄울 이유가 없다.
- **폼 삭제·응답 수정 도구.** 봇이 응답을 지울 수 있게 만들 이유가 없다.
