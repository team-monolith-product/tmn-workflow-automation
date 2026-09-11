# 매출 facts

매출장 구글시트 하나가 금액의 유일한 출처다. 이 폴더는 그 시트를 읽는 법과 분류 규칙이고,
코드는 `service/revenue/` 에 있다. **숫자를 여기서 새로 정의하지 않는다.**

## 파일

| 파일 | 무엇 | 언제 고치나 |
|---|---|---|
| `sources.yml` | 매출장 위치 · 탭별 회계연도와 열 배치 · 인식기준(발행 실적만) · 분류마스터 탭 위치 | 매출장 탭 이름이 바뀌거나 열이 밀렸을 때 |
| `taxonomy.yml` | 분류 뷰와 규칙. 2025년 이하 옛 16종 분류를 매출장 대분류 체계로 환산하는 `base` 와 대표 확인을 거친 예외 `exceptions` · 연도 총계 검증값 `assertions` | 분류 판정이 바뀌었을 때(`why` 에 근거를 적는다) · 연도가 마감돼 총계가 확정됐을 때 |
| `enrichment.yml` | CRM(노션 미러)에서 끌어온 거래처 보강값(학교급·교육청·예산출처)과 사업 마스터. `scripts/enrich_revenue_customers.py` 가 만든다 | 주 1회 손으로 돌리고 diff 를 보고 커밋한다. 사람이 확인한 값은 `manual:` 에만 적는다 |

## 확정된 기준 (2026-09-09 대표 결정 · 바꾸려면 대표 승인)

- 인식기준 = **세금계산서 발행 실적만.** 매출장 「예정」 행은 매출이 아니라 파이프라인으로 분리한다
- 금액 = 공급가액, 부가세 제외 · 귀속연도 = 매출장 탭 기준(발행일 연도와 다른 이연 건이 있다)
- 공개 범위 = 전 직원. 고객 실명·좌석수·단가·계약기간이 그대로 나간다 — 사내용이다

## 분류는 매출장이 정한다

대분류·세부분류의 정본은 매출장 **「분류마스터」 탭**이다. 항목을 늘리거나 이름을 바꾸려면 그 탭을
고친다. 빌드가 그 표를 읽어 뷰 순서와 `prefer_raw` 를 덮어쓰므로 `taxonomy.yml` 의 하드코딩 목록은
참고용이다. 단 `base` · `exceptions` 의 대상 이름은 덮어쓰지 않으므로, 시트에서 대분류 이름을 바꾸면
과거 연도가 유령 항목으로 새어 나간다 — 빌드가 「규칙이 «…»로 보내는데 분류마스터에 없다」로 잡는다.
대분류를 K열 너머로 늘리면 `sources.yml` 의 `detail_last_col` 도 키운다.

2026 은 매출장 I열 대분류를 그대로 쓰고(`prefer_raw`), 2025 이하만 규칙으로 같은 체계에 맞춘다.
세부분류(J열)는 2026 발행분에만 있다. **세부분류로 연도 비교를 하지 않는다.**

## 빌드가 스스로 검증하는 것

- 연도별 발행 총계가 `assertions.year_totals_issued` 와 5원 이내로 맞는가
- 규칙에 안 걸려 「미분류」로 떨어진 거래가 있는가
- 매출장 행이 분류마스터의 (대분류, 세부분류) 조합을 벗어났는가 · 규칙이 가리키는 대분류가 실재하는가
- 날짜를 못 읽은 행 · 사업 별칭이 가리키는 이름이 사업 마스터에 있는가

경고는 `revenue_health` 도구와 매일 07:10 슬랙 `t_관리_매출` 알림에 나간다.
**경고가 떠 있는 동안에는 그 숫자를 확정으로 쓰지 않는다.**

## 어디서 읽나

| 독자 | 길 |
|---|---|
| 직원의 Claude·Codex | Knowledge MCP `revenue_summary` → 필요하면 `revenue_transactions` · 의심되면 `revenue_health` |
| 매출 대시보드(별도 저장소) | `GET /revenue/facts.json` + `X-API-Key`. 여기서 받은 facts 로 HTML 만 굽는다. 다시 계산하지 않는다 |
| 슬랙 데이터봇 | 없다. 매출장을 OPERATING 계정에 공유하지 않았으므로 `read_sheet` 로 안 읽힌다 |

facts 는 프로세스 안에서 10분 캐시한다(`service/revenue/facts.py`). DB 에 넣지 않는다 — 지식베이스 DB 에
넣으면 `query_knowledge` 의 임의 SQL 로도 나가기 때문이다.

## 로컬에서 돌리기

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON='{...}'          # 매출장이 뷰어로 공유된 계정
python scripts/validate_revenue_ledger.py --dry-run  # 집계 출력 + 경고가 있으면 슬랙 메시지 미리보기

export REDASH_BASE_URL=https://redash.codle.io REDASH_API_KEY=...
python scripts/enrich_revenue_customers.py --dry-run # CRM 보강 커버리지만 확인
```
