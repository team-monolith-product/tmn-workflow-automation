# 구글폼 생성

선생님에게 정보를 받는 가장 쉬운 통로가 구글폼이다. 슬랙에서 "이런 폼 만들어줘"라고 말하면
봇이 폼을 만들어 공개하고 응답을 시트에 쌓는다.

지금은 이 일이 `industry-linked/create_*_form.py` 일곱 개에 복붙으로 흩어져 있다. 그중
**공개 처리를 둘 다 갖춘 것은 넷뿐이다.**

| 스크립트 | 링크 공개 | 게시 |
|---|---|---|
| create_demand_form / jitda_event / team / visit | O | O |
| create_staff_doc_form | O | **X** |
| create_survey_form | **X** | O |
| create_short_term_form | **X** | **X** |

사람이 매번 옮겨 적으니 매번 다르게 빠진다. 공개를 부르는 쪽의 선택지로 두는 한 이 표는 계속 나온다.

## API가 못 하는 것

셋 다 문서·실측으로 확인했다. 설계가 여기서 갈린다.

**하나. 응답 시트를 붙일 수 없다.** `Form.linkedSheetId`는 output-only다. 이미 붙어 있는
시트의 ID를 읽을 뿐, 써서 붙이지 못한다. 붙이는 것은 Apps Script `setDestination`뿐이고
그건 서비스 계정으로 못 돌린다.

**둘. `forms.gle` 단축 링크를 받을 수 없다.** `responderUri`는 100자짜리 원본 링크다.
단축은 폼 UI에만 있다. 지금 스크립트들이 `SHORT_URL` 상수에 사람이 손으로 붙여 넣는 이유가 이것이다.

**셋. `forms.create`가 서비스 계정에서 500으로 죽는다**(7/30 실측). Drive로
`application/vnd.google-apps.form` 파일을 만들고 `batchUpdate`로 채우면 된다. 이때 딸려 오는
기본 문항 하나를 **뒤에서부터** 먼저 지운다.

## 공개는 두 겹이고, 이제 안 하면 무조건 터진다

링크 공개(Drive `permissions.create` `anyone`/`reader`)와 게시(Forms `setPublishSettings`
`isPublished`+`isAcceptingResponses`)는 별개다. 하나만 해도 응답이 0건이다.

**2026년 7월 1일부터 API로 만든 폼은 기본이 미게시다.** 그 전에는 하위 호환으로 게시된 채
만들어졌다. 오늘(8/27)은 이미 지난 뒤라, 게시를 빼먹은 폼은 예외 없이 응답을 못 받는다.
`create_staff_doc_form`·`create_short_term_form` 방식은 지금 그대로 돌리면 죽는다.

## 설계

### 1. 공개는 생성의 일부다

`create_form()`이 문항·링크 공개·게시·되읽기 검증까지 마친 뒤에야 링크를 돌려준다. 공개에
실패하면 폼을 돌려주지 않고 예외를 던지며 인자로 끄고 켜지 못한다.

되읽기는 `create_jitda_event_form`이 하던 그대로다.

```python
perms = drive.permissions().list(fileId=fid, fields="permissions(type,role)",
                                 supportsAllDrives=True).execute()["permissions"]
assert any(p["type"] == "anyone" for p in perms), "링크 공개가 안 됐다"
state = svc.forms().get(formId=fid).execute()["publishSettings"]["publishState"]
assert state.get("isPublished") and state.get("isAcceptingResponses"), "게시가 안 됐다"
```

만드는 자리에서 확인하므로, 링크를 카톡에 뿌리기 전 슬랙 대화 안에서 문제가 드러난다.

### 2. 시트는 붙이지 말고 우리가 채운다

붙일 수 없으니 우리가 만든다. 폼을 만들 때 같은 폴더에 응답 스프레드시트를 같이 만들어 두면
스케줄러가 `forms.responses.list`로 읽어 그 시트에 적는다.

- 주기 10분, `config.yaml`의 `scheduled_jobs`에 건다
- 헤더는 `forms.get`의 문항 순서를 쓰고 그 아래로 응답을 적는다
- 전량 덮어쓰기. 폼 응답은 많아야 수백이다

폼 UI로 붙인 시트와 다른 점은 **최대 10분 지연**뿐이다. 사람이 보는 것은 똑같은 시트고 링크도
폼 생성 응답에 같이 준다.

Pub/Sub `watches`로 실시간을 만들 수도 있으나 인프라가 하나 늘어난다. 선생님 응답을 10분 안에
봐야 하는 일도 없다.

### 3. 단축 링크는 우리가 발급한다

`wfa.codle.io`는 ALB와 TLS를 달고 이미 인터넷에 열려 있는 FastAPI 엔드포인트라, 여기에
리다이렉트 한 줄만 붙이면 된다.

```
GET /f/{slug} → 302 responderUri
```

`wfa.codle.io/f/a3f9k2`면 22자다. 원본 100자는 EUC-KR 90바이트인 문자 한 통에 아예 안
들어가서 지금 폼 링크를 문자로 보내면 LMS로 넘어가고 통당 단가가 오른다.

bit.ly 같은 외부 단축기를 쓰지 않는 이유는 수명과 도메인 평판을 우리가 못 쥐기 때문이다.
국내 문자 스팸 필터는 낯선 단축 도메인을 곧잘 걸러내는데 우리 도메인이면 우리가 손을 쓴다.

### 4. 계정이 권한 경계다

`FORM_SERVICE_ACCOUNT_JSON`을 새로 판다. 기존 둘은 읽기 전용으로 그대로 둔다.

| 계정 | 스코프 | 보는 범위 |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 읽기 | 학교 일정 시트 |
| `OPERATING_SHEET_SERVICE_ACCOUNT_JSON` | 읽기 | 사업 운영 시트 |
| `FORM_SERVICE_ACCOUNT_JSON` | `drive`, `forms.body`, `spreadsheets` | **자기가 만든 폼과 시트뿐** |

`api/google_sheets.py`가 읽기 전용인 것은 의도다. 거기에 쓰기를 얹으면 봇이 사람이 관리하는
시트를 고칠 수 있게 된다. 새 계정은 쓰기를 갖지만 사업 시트 어디에도 공유돼 있지 않으므로
건드릴 것이 없다. 계정이 곧 범위라는, 이 레포가 이미 쓰는 방식 그대로다.

### 5. 파일 업로드 문항은 막는다

파일 업로드 문항이 있는 폼은 응답자가 구글 로그인을 해야 한다. 링크 공개와 양립하지 않는다.
빌더에서 거부하고 사본은 이메일로 받으라고 안내한다. `create_staff_doc_form`이 신분증·통장
사본을 이미 그렇게 받고 있다.

## 배치

```
api/google_forms.py              Forms·Drive REST 얇은 래퍼 (api/ 규칙: 래퍼만)
service/form.py                  생성 → 문항 → 공개 → 검증 → 시트 → 슬러그
app/tools/form_tools.py          create_form, list_forms (LangChain tool)
scripts/sync_form_responses.py   응답 → 시트. config.yaml 10분
main.py                          GET /f/{slug}
migrations/knowledge/004_form.sql
```

```sql
CREATE TABLE form (
    slug          text PRIMARY KEY,
    form_id       text NOT NULL UNIQUE,
    responder_uri text NOT NULL,
    sheet_id      text NOT NULL,
    title         text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    synced_at     timestamptz
);
```

`responder_uri`를 저장하는 이유는 그 URL의 ID가 `form_id`와 다른 값이라서다. 조합해서 만들 수 없다.

문항 스펙은 기존 스크립트의 `QUESTIONS` 튜플을 그대로 스키마로 옮긴다.

```python
(title, description, kind, required, options)
# kind: short | para | radio | check | dropdown | scale | date  (file 없음)
```

같은 제목의 폼이 있으면 재사용한다. 기존 스크립트 전부가 하던 것이고 슬랙에서 두 번 부르면
폼이 두 개가 되는 사고를 막는다.

## 안 하는 것

- **승인 게이트.** 문자와 달리 폼은 만든 뒤 고칠 수 있다. 오타 하나에 카드를 한 장 더 띄울 이유가 없다.
- **폼 삭제·응답 수정 도구.** 봇이 응답을 지울 수 있게 만들 이유가 없다.
- **Apps Script 배포.** 시트를 진짜로 붙이려면 이 길뿐인데, 도메인 위임과 배포 파이프라인이
  따라온다. 10분 지연으로 되는 일이다.
