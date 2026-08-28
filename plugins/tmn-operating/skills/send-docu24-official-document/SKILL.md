---
name: send-docu24-official-document
description: 문서24에서 보낸 공문을 먼저 검색해 재작성하거나, 재활용할 공문이 없을 때 새 공문을 Playwright로 준비·검토·발송할 때 사용합니다. 운영팀의 대외 공문 발송 업무에 적용합니다.
---

# Send Docu24 Official Document

문서24 공문 발송은 `scripts/docu24`의 Playwright runner로 수행한다. LLM은 검색 조건·본문·변경안만 만들고, 페이지 이동·기관 선택·파일 업로드·발송은 runner가 검증 가능한 locator로 수행한다.

## 실행 순서

1. 먼저 현재 요청에서 제목·기존 수신기관 기준의 `search.json`을 만들고 보낸 문서함을 검색한다.
2. 후보가 있으면 운영자에게 문서번호·제목·수신기관·발송일·첨부 유무를 보여 주고 재사용할 공문을 하나 선택하게 한다.
3. 선택한 공문이 있으면 `reuse` 작업으로, 없으면 `new` 작업으로 `prepared-job.json`을 만든다.
4. `prepare`는 PDF 미리보기까지 열고 멈춘다. 화면에서 수신기관·내용·첨부를 검토한다.
5. 실제 발송은 미리보기의 요약 ID와 일치하는 `approval.json`에 `confirmSend: true`가 있을 때만 `send`로 진행한다.

```bash
cd plugins/tmn-operating
npm install
npm run docu24 -- search --job search.json --profile-dir /absolute/docu24-browser-profile
npm run docu24 -- prepare --job prepared-job.json --profile-dir /absolute/docu24-browser-profile
npm run docu24 -- send --job prepared-job.json --approval approval.json --profile-dir /absolute/docu24-browser-profile
```

전용 `profile-dir`은 사용자별로 정한다. 첫 실행에는 보이는 Chrome에서 문서24 로그인을 사용자가 직접 완료한다. 기존 개인 Chrome 프로필을 그대로 쓰지 않는다.

## 입력 규칙

`search.json`에는 `titleTerms` 또는 `recipientTerms` 중 하나 이상을 넣는다.

```json
{
  "titleTerms": ["강사위촉"],
  "recipientTerms": ["고등학교"]
}
```

재활용 공문은 수신기관과 본문을 새 요청값으로 교체한다. 제목은 바꿀 필요가 있을 때만 `title`에 넣는다. 기존 첨부·직인·발신자·제출자 설정은 유지하며, 미리보기에서 현재 공문에도 유효한지 검토한다.

신규 공문은 `title`, `body`, `attachments`, `confirmSubmissionChecks: true`가 필요하다. 첨부파일은 절대 경로를 사용하고 runner가 존재 여부·총 500MB 제한을 확인한다.

신규 공문 작업에는 같은 `sentDocumentSearch`와 운영자의 `reuseDecision: "no-reusable-candidate"`도 반드시 넣는다. runner는 `prepare` 직전에 보낸 문서함을 다시 검색해 이 workflow가 생략되지 않았음을 실행 결과에 남긴다.

## 중단 규칙

- 검색·기관 선택·재활용 대상이 여러 개면 추측하지 않고 운영자에게 선택을 요청한다.
- 로그인, 인증서, CAPTCHA, 보안 경고는 사용자가 처리한다.
- `보내기`를 누른 뒤에는 자동 재시도하지 않는다. 보낸 문서함에서 동일 문서번호·제목·수신기관을 확인한 뒤에만 다음 조치를 결정한다.
