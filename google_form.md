# 구글폼 생성

선생님에게 정보를 받는 가장 쉬운 통로가 구글폼이다. 슬랙에서 "이런 폼 만들어줘"라고 말하면
봇이 만들어 공개하고 사람이 마지막 두 번 클릭할 링크를 같이 준다.

지금은 이 일이 `industry-linked/create_*_form.py` 일곱 개에 복붙으로 흩어져 있고,
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
실패하면 예외를 던지며 인자로 끄고 켜지 못한다. 되읽기는 `create_jitda_event_form` 그대로다.

```python
perms = drive.permissions().list(fileId=fid, fields="permissions(type,role)",
                                 supportsAllDrives=True).execute()["permissions"]
assert any(p["type"] == "anyone" for p in perms), "링크 공개가 안 됐다"
state = svc.forms().get(formId=fid).execute()["publishSettings"]["publishState"]
assert state.get("isPublished") and state.get("isAcceptingResponses"), "게시가 안 됐다"
```

### 2. 남은 두 번은 링크로 넘긴다

무엇을 눌러야 하는지까지 답에 박아 준다.

```
폼을 만들었습니다. 링크를 뿌리기 전에 두 가지만 눌러 주세요.

  ① 응답 시트 연결  https://docs.google.com/forms/d/{formId}/edit#responses
     '응답' 탭 → 시트 아이콘 → [새 스프레드시트 만들기]

  ② 짧은 주소 받기  https://docs.google.com/forms/d/{formId}/edit
     오른쪽 위 [보내기] → 링크 아이콘 → 'URL 단축' 체크 → 복사

  공개 상태 : 링크 공개 O · 게시 O
  응답 링크 : https://docs.google.com/forms/d/e/{responderId}/viewform
```

②가 필요한 이유는 길이다. 원본 응답 링크는 100자라 EUC-KR 90바이트인 문자 한 통에 안
들어가서, 단축 없이 보내면 LMS 로 넘어가 통당 단가가 오른다.

### 3. 편집자 공유는 선택이 아니다

봇이 만든 폼은 서비스 계정 소유라 공유하지 않으면 아무도 못 연다. 편집 권한이 없으면 위 두
번을 누를 자격이 없다. 기본 편집자는 `config.yaml`에 두고 tool 인자로 더한다.

### 4. 계정이 권한 경계다

`FORM_SERVICE_ACCOUNT_JSON` 을 새로 판다. 스코프는 `drive`·`forms.body` 둘뿐이다. 시트를
사람이 붙이므로 `spreadsheets` 는 필요 없다.

`api/google_sheets.py` 가 읽기 전용인 것은 의도이므로 거기에 쓰기를 얹지 않는다. 새 계정은
사업 시트 어디에도 공유돼 있지 않아 자기가 만든 폼 말고는 건드릴 것이 없다. 계정이 곧
범위라는, 이 레포가 이미 쓰는 방식 그대로다.

폼은 **공유 드라이브 폴더 안에** 만든다. 서비스 계정에는 개인 드라이브 저장용량이 없어 그
밖에서는 파일 생성 자체가 실패한다.

### 5. 파일 업로드 문항은 막는다

파일 업로드가 있으면 응답자가 구글 로그인을 해야 해서 링크 공개와 양립하지 않는다. 빌더에서
거부하고 사본은 메일로 받으라고 안내한다. `staff_doc` 이 이미 그렇게 받고 있다.

## 배치

```
api/google_forms.py       Forms·Drive REST 얇은 래퍼 (api/ 규칙: 래퍼만)
service/form.py           생성 → 문항 → 공개 → 편집자 → 검증 → 안내문
app/tools/form_tools.py   create_form (LangChain tool)
```

새 테이블도 엔드포인트도 스케줄도 없다. 같은 제목의 폼이 있으면 재사용한다. Drive `files.list`
로 찾되 `trashed=false` 와 공유 드라이브 플래그가 필요하다. 두 함정 모두
`api/google_sheets.py` 의 `list_spreadsheet_files` 에 적혀 있다.

문항 스펙은 기존 스크립트의 `QUESTIONS` 튜플을 그대로 옮긴다.

```python
(title, description, kind, required, options)
# kind: short | para | radio | check | scale
```

일곱 스크립트가 실제로 쓴 유형이 이 다섯이다. `dropdown`·`date` 는 한 번도 안 썼으므로 넣지
않는다.

## 안 하는 것

- **응답 동기화.** 사람이 한 번 누르면 구글이 실시간으로 붙여 준다. 우리가 퍼 나르면 지연에
  더해 스케줄·쿼터·헤더 변경까지 떠안는다.
- **자체 단축 링크.** 사람이 진짜 `forms.gle` 을 받는다.
- **`list_forms` tool.** 만든 직후 안내로 끝난다. 목록이 필요해지면 그때 붙인다.
- **승인 게이트.** 문자와 달리 폼은 만든 뒤 고칠 수 있다.
- **폼 삭제·응답 수정 도구.**
