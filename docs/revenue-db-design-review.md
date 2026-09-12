# 매출 DB 구조 — 승인된 구현

구글 매출장 + CRM(Athena/Redash) → 일간 배치 → PostgreSQL → Query Knowledge / 대시보드.

DB는 재생성 가능한 캐시다. **revenue_transactions 한 테이블**만 둔다.
실행 이력 테이블, 분류 테이블, 뷰, ID, 원본 JSON, facts 및 메모리 캐시는 없다.

| 컬럼 | 타입 | 의미 |
|---|---|---|
| year | integer | 매출장 귀속연도 |
| issued_on | date | 발행일. 예정이면 NULL |
| status | text | issued / planned |
| customer, item | text | 거래처·품목 |
| quantity, unit_price | numeric | 수량·단가 |
| amount, tax, total | numeric | 공급가액·세액·합계 |
| category, subcategory | text | 정규화 상품 분류·세부분류 |
| note | text | 비고 |
| school_level, edu_office | text | 학교급·교육청 |
| budget_sources, terms | text[] | 고객 CRM의 예산출처·사용학기 목록 |
| program_name, program_client | text | 사업명·발주처 |
| program_budget, program_our_revenue | numeric | 사업 전체 예산·우리 몫 |
| program_stage | text | 사업 단계 |

배치는 매일 07:10 KST에 전체를 읽고, 정규화·분류·CRM 연결을 마친 다음
한 트랜잭션에서 DELETE + INSERT한다. 읽기/파싱/CRM 조회/적재 실패 시 기존 데이터를 유지한다.
동일한 원본 행은 임의로 중복 제거하지 않는다. 동시 배치는 DB advisory lock으로 직렬화한다.
오류는 기존 로그/Sentry, 업무 검증 경고는 기존 Slack 경로로 알린다.

분류·수기 보정·사업 별칭은 `knowledge/revenue/rules.yml`에 둔다.
분류마스터는 배치에서만 읽어 원본 분류를 확인한다. DB 테이블로 복제하지 않는다.
CRM 조회 결과 파일은 없고 연결 결과만 거래에 저장한다. 매칭되지 않으면 비워 둔다.
LLM API는 사용하지 않는다.

화면은 SQL 집계와 거래 100건 단위 조회를 사용한다. SQL 실행마다 DB를 읽고 외부 소스를 조회하지 않는다.
Query Knowledge는 같은 테이블을 읽으며 매출 전용 MCP 도구는 제거한다.
매출은 `status='issued'`인 `amount`로 집계한다. `planned`는 별도다.

CRM 예산출처/사용학기는 고객 수준 정보이며 개별 거래의 확정 예산/계약기간이 아니다.
복수 예산출처별 매출은 겹칠 수 있다. 사업 전체 금액도 거래마다 반복되므로 합산하지 않는다.

실제 SQL: `migrations/knowledge/006_revenue_transactions.sql`.
운영 DB 마이그레이션·최초 동기화는 코드 검증과 별개의 배포 단계다.
