"""
단계 간 계약 스키마 (Announcement / Evaluation / Decision)

이 세 스키마만 고정하면 각 단계 내부 구현은 독립적으로 진화할 수 있다.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

# 결정 라벨 (S6). 보고·S4 정독 대상은 REPORTABLE_LABELS (우선순위 순서이기도 함).
LABEL_RECOMMEND = "입찰추천"
LABEL_REVIEW = "검토"
LABEL_FUTURE = "미래타깃"
LABEL_EXCLUDE = "제외"
REPORTABLE_LABELS = (LABEL_RECOMMEND, LABEL_REVIEW, LABEL_FUTURE)


@dataclass
class Announcement:
    """정규화된 입찰공고 (S1 출력)."""

    kind_label: str
    bid_no: str
    bid_ord: str
    title: str
    notice_inst: str  # 공고기관
    demand_inst: str  # 수요기관
    close_dt: str
    estimated_price: str  # 추정가격
    url: str
    # 선별 신호 (목록 응답에 포함) — 소스에 따라 없을 수 있어 기본값 "".
    award_method: str = ""  # 낙찰자결정방법 (sucsfbidMthdNm)
    re_notice: str = ""  # 재공고여부 (reNtceYn)
    result_competition: str = ""  # 실적경쟁여부 (arsltCmptYn) — 실적제한 신호
    industry_limit: str = ""  # 업종제한여부 (indstrytyLmtYn)
    region_limit_basis: str = ""  # 지역제한 판단기준명 (rgnLmtBidLocplcJdgmBssNm)
    tech_eval_rate: str = ""  # 기술능력평가비율 (techAbltEvlRt)
    price_eval_rate: str = ""  # 입찰가격평가비율 (bidPrceEvlRt)
    info_biz: str = ""  # 정보화사업여부 (infoBizYn)
    service_div: str = ""  # 용역구분명 (srvceDivNm)
    proc_class: str = ""  # 조달 소분류 (pubPrcrmntClsfcNm)
    proc_mid: str = ""  # 조달 중분류 (pubPrcrmntMidClsfcNm)
    proc_large: str = ""  # 조달 대분류 (pubPrcrmntLrgClsfcNm)
    work_type: str = ""  # 사업유형 태그 (S3에서 분류, 개발/운영/교육운영/...)
    stage: str = "notice"  # notice=본공고 | presearch=사전규격
    opinion_close_dt: str = ""  # 사전규격 의견등록 마감일시 (영업·의견제출 윈도우)
    spec_docs: list[dict] = field(default_factory=list)  # 규격서 첨부 [{name, url}]


@dataclass
class GateResult:
    """참가 가능성 게이트 결과 (S2 출력)."""

    status: str  # pass | near_miss | fail
    reasons: list[str] = field(default_factory=list)


# --- S5 LLM 출력 ---


class Axes(BaseModel):
    """4축 점수 (0~100)."""

    reuse: int = Field(
        ge=0, le=100, description="재사용률(우리 자산으로 저렴하게 수행 가능한 정도)"
    )
    winnability: int = Field(
        ge=0, le=100, description="수주가능성(정량장벽 낮음·낙찰방식·경쟁·기관친숙도)"
    )
    value: int = Field(ge=0, le=100, description="사업가치(체급 적합·LTV)")
    performance_building: int = Field(
        ge=0, le=100, description="정량 실적 적립 가치(깨끗한 직접 용역계약일수록 높음)"
    )


class EvalOut(BaseModel):
    """공고 1건 LLM 평가 (S5)."""

    index: int = Field(description="입력 공고 index")
    axes: Axes
    quant_barrier: str = Field(
        description="정량(실적) 장벽: none | low | med | high | unknown"
    )
    wired_risk: str = Field(
        description="내정위험(사전영업·규격 lock-in·기존업체 유리로 사실상 정해진 정도): none | low | med | high | unknown"
    )
    matched_assets: list[str] = Field(
        default_factory=list, description="실제 적용 가능한 우리 자산 id"
    )
    rationale: str = Field(description="핵심 근거 한두 줄")


class BatchEval(BaseModel):
    evaluations: list[EvalOut]


@dataclass
class Decision:
    """최종 결정 (S6 출력)."""

    announcement: Announcement
    gate: GateResult
    axes: dict
    quant_barrier: str
    matched_assets: list[str]
    score: float
    label: str  # 입찰추천 | 검토 | 미래타깃 | 제외
    rationale: str
    wired_risk: str = "unknown"  # 내정위험 (none/low/med/high/unknown)
    enriched: bool = False  # S4 규격서 정독 후 재평가되었는지
