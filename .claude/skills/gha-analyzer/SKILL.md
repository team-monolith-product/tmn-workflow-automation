---
name: gha-analyzer
description: Analyze GitHub Actions workflow failures and suggest fixes. Use when you receive a GHA run or job URL to diagnose CI/CD failures.
allowed-tools:
  - Bash(gh:*)
  - Bash(git:*)
  - Read
  - Grep
  - Glob
---

# GitHub Actions Failure Analyzer

GitHub Actions 워크플로우 실패를 분석하고 해결책을 제시하는 skill입니다.

## URL 형식

다음 형식의 URL을 지원합니다:
- Run URL: `https://github.com/{owner}/{repo}/actions/runs/{run_id}`
- Job URL: `https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id}`

## Analysis Workflow

### Step 1: URL 파싱

URL에서 다음 정보를 추출합니다:
- `owner/repo`: 저장소 정보
- `run_id`: 워크플로우 실행 ID
- `job_id` (optional): 특정 job ID

### Step 2: Run 정보 조회

```bash
gh run view {run_id} --repo {owner}/{repo}
```

이 명령으로 다음을 확인합니다:
- 워크플로우 이름
- 트리거 방식 (push, pull_request, schedule 등)
- 실패한 job 목록
- 각 step의 성공/실패 상태

### Step 3: Job 상세 정보 조회

```bash
gh api repos/{owner}/{repo}/actions/runs/{run_id}/jobs
```

JSON 응답에서 실패한 step을 식별합니다:
- `conclusion: failure`인 step 찾기
- step 이름과 번호 기록

### Step 4: 실패 로그 가져오기

```bash
gh api repos/{owner}/{repo}/actions/jobs/{job_id}/logs
```

로그에서 에러 메시지를 추출합니다. 찾아볼 패턴:
- `##[error]` - GitHub Actions 에러 마커
- `Error:`, `ERROR`, `Exception`, `Traceback`
- `exit code 1` 또는 다른 비정상 종료 코드
- `FAILED`, `FAILURE`

### Step 5: 관련 코드 확인

실패한 step이 스크립트 실행인 경우:
1. 해당 스크립트 파일 읽기
2. 워크플로우 YAML 파일 확인 (`.github/workflows/`)
3. 에러와 관련된 코드 라인 식별

```bash
# 워크플로우 파일 찾기
ls .github/workflows/

# 특정 워크플로우 읽기
cat .github/workflows/{workflow_name}.yml
```

### Step 6: 원인 분석 및 해결책 제시

## Output Format

### 분석 결과 요약

| 항목 | 값 |
|------|-----|
| Workflow | [name] |
| Run ID | [id] |
| 실패 Job | [job name] |
| 실패 Step | [step name] |
| 에러 타입 | [type] |

### 에러 로그

```
[핵심 에러 메시지 발췌]
```

### 원인 분석

[에러의 근본 원인 설명]

### 해결 방안

1. [첫 번째 해결 방안]
2. [두 번째 해결 방안 (있다면)]

### 수정 코드 (필요시)

```python
# 수정이 필요한 코드 예시
```

## Common Failure Patterns

### Python 스크립트 에러
- `ModuleNotFoundError`: requirements.txt에 패키지 추가 필요
- `KeyError`: 환경 변수 또는 API 응답 필드 누락
- `TypeError`: 함수 인자 불일치

### GitHub Actions 설정 에러
- `Invalid workflow file`: YAML 문법 오류
- `Resource not accessible`: 권한 부족 (GITHUB_TOKEN permissions)

### 환경 변수 에러
- `secrets.*` 미설정
- 환경 변수 이름 오타

### 의존성 에러
- 버전 충돌
- 패키지 설치 실패
