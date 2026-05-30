# 교육 외주 입찰 파이프라인 — 구조 설계 (draft v0)

> 목적: 팀모노리스의 기보유 개발 자산·역량으로 **효율적으로 수행 가능**하고 **수주 확률이 높은**
> 공공 입찰 공고를 매일 자동으로 선별한다.
>
> 이 문서는 구현보다 먼저, 각 단계가 **독립적으로 진화**하도록 구조를 고정하기 위한 것이다.

## 0. 설계 원칙 — 메커니즘과 지식의 분리

우리의 자산·자격·실적·전략 가중치는 계속 변한다. 따라서 변하는 것을 코드에 박지 않는다.

- 파이프라인 레이어 (안정적 메커니즘): 수집 → 정규화 → 게이트 → 트리아지 → 보강 → 평가 → 결정 → 보고.
  단계의 **책임과 입출력 계약**은 잘 변하지 않는다.
- 지식 레이어 (자주 바뀌는 데이터): 우리 역량, 자격·실적, 점수 정책, 소스 목록.
  파일로 외부화하고 버전을 매긴다.
- 회사가 변하면 → **지식 파일만** 갱신한다. 단계 코드는 그대로.
- 단계 간에는 **공통 스키마**로만 연결한다. 한 단계를 교체·고도화해도 다른 단계는 영향 없음.

```
            ┌─────────────────────────  지식 레이어 (변동)  ─────────────────────────┐
            │  capability_profile   eligibility_ledger   scoring_policy   source_registry │
            └───────────▲───────────────▲──────────────────▲───────────────▲────────────┘
                        │ inject         │ inject            │ inject        │ inject
  수집 → 정규화/dedupe → [게이트] → [트리아지] → 보강 → [심층평가] → 결정/랭크 → 보고(Slack/Notion)
   S0       S1            S2          S3        S4        S5          S6          S7
                                                                       │
                                              결과 원장(S8) ───피드백──▶ eligibility_ledger 갱신
```

## 1. 지식 레이어 (변동 자산)

각 파일은 `version`, `updated`, 갱신 주체를 가진다. 단계는 이 파일을 **읽기만** 한다.

### 1.1 `capability_profile` — 우리 역량·자산
우리가 재사용·수행할 수 있는 것. 자산이 늘거나 성숙도가 바뀌면 갱신.
```yaml
version: 1
assets:
  - id: codle
    name: 코들 K12 코딩·SW교육 플랫폼(LMS)
    covers: [LMS, 교사저작, 학생관리, 채점리포트, 코스운영]
    keywords: [코딩교육, SW교육, 교수학습플랫폼, 에듀테크, 학습관리시스템]
    maturity: production            # production | beta | poc
  - id: jupyter_judge
    name: JupyterHub 실습환경 + 자동채점(judge)
    covers: [클라우드 코딩실습, 온라인 평가, 자동채점]
    keywords: [코딩실습, 자동채점, 온라인평가, 클라우드 실습환경, 프로그래밍 평가]
    maturity: production
  - id: block_coding
    name: entry/pxt 블록코딩·피지컬컴퓨팅
    keywords: [블록코딩, 초등 SW, 피지컬컴퓨팅, 마이크로비트]
    maturity: production
  - id: ai_tutor
    name: 생성형 AI 학습보조
    keywords: [AI 디지털교과서, AI 튜터, 생성형 AI 교육]
    maturity: beta
  - id: content
    name: SW·AI 교육 콘텐츠 저작
    keywords: [교육콘텐츠, 교재개발, 커리큘럼]
    maturity: production
```
- S3(트리아지)의 키워드 매핑, S5(심층평가)의 재사용률 판정 기준선.
- 진화: 새 제품/모듈 추가 = 항목 추가. 베타→프로덕션 승격 = maturity 변경.

### 1.2 `eligibility_ledger` — 자격·실적 원장 (append-only)
하드 게이트의 근거. **수주할 때마다 실적이 쌓이는 살아있는 원장**.
```yaml
version: 1
credentials:
  sw_business_report: true      # SW사업자 신고
  direct_production_cert: true  # 직접생산증명
  region: 서울
performance:                    # append-only, 수주 시 한 줄 추가
  - project: ○○ 코딩교육 플랫폼 구축
    domain: SW교육
    amount: 120000000
    year: 2025
    client: ○○교육청
# totals_by_domain 은 performance 에서 파생 계산 (분야별 누적액·건수)
```
- S2(게이트)의 실적요건 충족 판정, S5의 "실적 적립 가치" 산정에 사용.
- 진화: S8 결과 원장에서 **수주 건이 자동으로 여기에 append** → 다음부터 더 많은 게이트가 열린다. (핵심 진화 엔진)

### 1.3 `scoring_policy` — 점수·결정 정책
전략 가중치. 사업 단계가 바뀌면 갱신.
```yaml
version: 1
ticket_size: { min: 50000000, sweet_low: 50000000, sweet_high: 1000000000 }  # 5천만~10억
award_method_stance:
  협상에의한계약: prefer              # 기술평가 = 우리 무대
  적격심사: conditional_on_reuse      # 재사용률 높으면 가격경쟁력으로 참여
  최저가: conditional_on_reuse
weights:                            # 균형(EV × 전략가치)
  reuse: 0.35                       # 재사용률(효율·마진) — 1급 신호
  winnability: 0.30                 # 수주가능성
  value: 0.20                       # 사업가치(규모·LTV)
  performance_building: 0.15        # 실적 적립 가치(실적 약점 보강 투자)
thresholds: { recommend: 70, review: 50, future_target: 0 }
near_miss_ratio: 0.7               # 실적요건의 70%+ 이면 버리지 말고 '미래타깃'
```
- S5 프롬프트 가중, S6 결정 임계.
- 진화: 실적이 충분해지면 `performance_building` 비중을 낮추는 식으로 단계 전환.

### 1.4 `source_registry` — 수집 소스 목록
```yaml
sources:
  - { id: g2b_servc, adapter: g2b, kind: servc, enabled: true }   # 나라장터 용역
  - { id: g2b_thng,  adapter: g2b, kind: thng,  enabled: false }  # 물품
  - { id: s2b,       adapter: s2b,               enabled: false } # 학교장터(미구현)
```
- S0가 읽어 어댑터를 켠다. 진화: 새 소스 = 어댑터 추가 + 한 줄 등록.

## 2. 파이프라인 레이어 (단계별 계약)

| 단계 | 책임 | 입력 | 출력 | 읽는 지식 | 진화 포인트 |
|---|---|---|---|---|---|
| S0 수집 | 소스별 공고 목록 조회 | 조회구간 | raw 공고[] | source_registry | 소스 어댑터 추가 |
| S1 정규화 | 필드 정규화·차수/교차 dedupe | raw[] | `Announcement[]` | — | 스키마 매핑 보강 |
| S2 게이트 | 참가 가능성 정량 판정 | Announcement[] | pass/fail/near-miss + 사유 | eligibility_ledger | 자격·실적 변동 시 자동 변화 |
| S3 트리아지 | 명백 무관 제거(저비용 깔때기) | 게이트 통과[] | candidate[] | capability_profile | 키워드 맵 갱신 |
| S4 보강 | 숏리스트만 상세 fetch(규격서·면허·지역) | candidate[] | enriched[] | source_registry | 상세 오퍼레이션 추가 |
| S5 심층평가 | LLM 4축 점수+근거 | enriched[] | `Evaluation[]` | 3종 지식 전부 | 프롬프트=지식 조립, 가중 변경 |
| S6 결정 | 축 종합→우선순위·라벨 | Evaluation[] | `Decision[]` | scoring_policy | 임계·가중 조정 |
| S7 보고 | sink 전송 | Decision[] | — | (sink 설정) | Slack→Notion→대시보드 |
| S8 결과원장 | 입찰/낙찰 결과 기록·피드백 | 수동/외부 입력 | 원장 갱신 | → eligibility_ledger | 진화 루프 폐쇄 |

### 공통 스키마 (단계 간 계약)
- `Announcement`: 출처·공고번호·차수·제목·공고/수요기관·게시/마감/개찰일시·추정가격·낙찰방식·재공고여부·상세URL·raw.
- `Evaluation`: announcement_ref + `gate{status,reasons}` + `axes{reuse,winnability,value,performance_building}` + `score` + `rationale`.
- `Decision`: evaluation_ref + `label`(입찰추천|검토|미래타깃|제외) + `priority` + 요약.

이 세 스키마만 고정하면, 각 단계의 내부 구현(규칙→ML→다른 LLM 등)은 자유롭게 진화한다.

## 3. 진화 루프 (왜 이 구조가 "진화 가능"한가)

1. 자산이 늘면 → `capability_profile` 갱신 → S3/S5가 더 많은 공고를 적합으로 인식.
2. 수주하면 → S8이 `eligibility_ledger.performance`에 append → S2 게이트가 자동으로 더 많이 열림.
3. 전략 단계가 바뀌면 → `scoring_policy.weights` 조정(예: 실적 충분 시 performance_building↓).
4. 새 시장이 열리면 → `source_registry`에 어댑터 등록.

→ 네 가지 변화 모두 **지식 파일 한 곳**만 건드린다. 파이프라인 단계 코드는 불변.

## 3.5 핵심 전략 (현 단계: 실적 약함) — `scoring_policy.strategy`

- 전략 1 (primary): 정량(실적) 점수 요구가 낮거나 없는 공고 × 우리 자산으로 저렴하게
  만들 수 있는(재사용률 높은) 건을 최우선. 정량 배점 싸움을 피하고 저원가로 이긴다.
- 전략 2 (secondary): 깨끗한 직접 용역계약(계약금액 명시·실적증명 가능)으로 정량 실적을
  쌓아주는 건은 미래 장벽 해소 투자로 가점. (RS·이용계약 매출은 정량실적 안 됨)
- 탐지 위치: "정량 장벽(실적제한·정량배점)"은 목록 단계에 안 나오므로 S4 보강에서 규격서·
  평가배점표·실적기준을 추출해 S5가 `winnability.quant_barrier`로 점수화. S2 게이트는
  '실적요건 없음=무조건 통과' vs '실적요건 미달=near-miss(미래타깃)'를 구분한다.

## 4. 비용·성능 구조 (깔때기)

678건/일(용역) 전수를 그대로 LLM에 넣지 않는다.
- S2 게이트 + S3 트리아지(둘 다 비-LLM, 저비용)로 수십 건까지 축소.
- S4 보강 + S5 LLM 심층평가는 **숏리스트에만** → 비용이 후보 수에 비례.

## 5. 현재 구현 → 이 구조로의 매핑

지금은 단일 스크립트(S0·S1·S5-lite·S7)가 한 덩어리. 분해 경로:
- `api/` : 소스 어댑터(`g2b.py` 존재, 향후 `s2b.py` 등) — S0.
- `service/edu_bid/` (신설 제안): `normalize`(S1) `gate`(S2) `triage`(S3) `enrich`(S4) `evaluate`(S5) `decide`(S6) `sinks`(S7) + `schemas`(공통 계약).
- `knowledge/edu_bid/` (신설 제안): `capability_profile.yaml` `eligibility_ledger.yaml` `scoring_policy.yaml` `source_registry.yaml`.
- `scripts/crawl_education_bids.py` : 위 단계를 순서대로 호출하는 **얇은 오케스트레이터**로 축소.

## 6. 열린 결정 사항 (다듬을 것)

- [ ] 지식 파일 포맷·위치: `knowledge/edu_bid/*.yaml` 로 가는가.
- [ ] S2 게이트를 어디까지 정량 자동화 vs S5 LLM에 위임할지 경계.
- [ ] S8 결과 원장 입력 방식: 수동(노션/슬랙 명령) vs 반자동.
- [ ] near-miss(미래타깃) 추적을 별도 sink(노션 DB)로 둘지.
