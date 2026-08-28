## 분석 절차

1. Grafana Slack 알림에서 pod 이름과 재시작 시각을 추출한다.
2. `list_log_streams`로 재시작 직전에 끝난 CloudWatch 로그 스트림을 찾는다.
3. `find_incomplete_requests`로 선택한 스트림에서 시작됐지만 완료되지 않은 요청을 찾는다.
4. 의심스러운 경로가 있으면 `query_alb_access_logs`로 요청·응답 크기를 확인한다.
5. 시간, payload, 처리 시간과 동시성에서 비정상적인 근거를 요약한다.

## 결과 형식

### 근거

- Pod
- 분석 시간 범위

### 의심스러운 요청

- Endpoint
- 미완료 요청 수
- 요청 payload 범위
- 응답 payload 범위

### 비정상적인 점

- 정상 패턴과 달랐던 점을 구체적으로 설명한다.
- 확인되지 않은 추측은 사실과 구분한다.
