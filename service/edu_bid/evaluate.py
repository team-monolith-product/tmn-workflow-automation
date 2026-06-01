"""
S5 심층평가 — LLM 이 지식(역량·자격·전략)을 주입받아 4축으로 점수화.

프롬프트는 지식 레이어에서 조립되므로, 회사가 변하면 코드가 아니라 YAML 만 바뀐다.
"""

from langchain_openai import ChatOpenAI

from .schemas import Announcement, BatchEval, EvalOut

_CRITERIA = [
    "# 평가 기준(축)",
    "- reuse: 위 자산으로 요구를 저렴하게 충족하는 정도(매칭 자산이 핵심 기능을 덮을수록↑)",
    "- winnability: 정량(실적)장벽이 낮을수록↑(실적경쟁=Y, 가격평가율 높고 실적 요구면↓), 협상·기술평가 무대면↑, 재공고/유찰↑, 교육기관 발주↑. wired_risk가 high일 때만 크게↓(med는 소폭, low/none은 감점 없음).",
    "- value: 체급 sweet spot 부합·후속 유지보수 가능성",
    "- performance_building: 깨끗한 직접 용역계약으로 '정량 실적금액'을 쌓아주면↑(단순 이용/물품/RS면↓)",
    "- quant_barrier: 정량 실적장벽 수준 none|low|med|high|unknown (실적경쟁=Y/실적요건 명시면 보통 high)",
    "- wired_risk: 사전영업·내정 가능성 none|low|med|high|unknown. 중요: '협상에 의한 계약'이라는 낙찰방식 자체는 내정 신호가 아니다(지식기반 용역의 표준). 그것만으로 올리지 말 것. 제목의 '고도화/연장'만으로도 올리지 말 것(정상 신규 경쟁일 수 있음). med/high 는 규격서에 구체적 독소조항이 있을 때만: 특정 제품·특정 업체 지정, 기존 시스템과 동일 벤더 기술지원 확약서 요구, 한두 업체만 충족할 좁은 실적·인증 요건, 자격 조건이 지나치게 많아 사실상 소수 업체만 통과 가능, 정량 평가 배점이 지나치게 구체적이어서 특정 업체에 유리하게 설계됨. 근거 없으면 low, 규격 미확인이면 unknown.",
    "교육/이러닝/에듀테크/SW·AI교육/디지털교과서와 무관하면 reuse를 낮게.",
]


def _assets_block(knowledge) -> str:
    lines = []
    for a in knowledge.assets:
        covers = ", ".join(a.get("covers", []))
        lines.append(
            f"- [{a['id']}] {a['name']} (성숙도 {a.get('maturity')}): {covers}"
        )
    return "\n".join(lines)


def _eligibility_block(knowledge) -> str:
    e = knowledge.eligibility_ledger
    cred = e.get("credentials", {})
    perf = e.get("performance", []) or []
    dp = ", ".join(d["name"] for d in cred.get("direct_production", []))
    inds = ", ".join(r["name"] for r in cred.get("industry_registrations", []))
    certs = ", ".join(c.get("name") for c in cred.get("certifications", []))
    perf_state = (
        f"{len(perf)}건"
        if perf
        else "정량 실적금액 약함(직접 용역계약 부족, RS·이용계약 위주)"
    )
    return (
        f"- 소재지: {cred.get('region')} | SW사업자신고={cred.get('sw_business_report')}\n"
        f"- 업종등록(참가자격): [{inds}]\n"
        f"- 직접생산확인: [{dp}]\n"
        f"- 인증: [{certs}]\n"
        f"- 정량 실적 상태: {perf_state}"
    )


def _strategy_block(knowledge) -> str:
    s = knowledge.scoring_policy.get("strategy", {})
    p = s.get("primary", {})
    sec = s.get("secondary", {})
    ts = knowledge.scoring_policy.get("ticket_size", {})
    stance = knowledge.scoring_policy.get("award_method_stance", {})
    stance_str = ", ".join(f"{k}:{v}" for k, v in stance.items())
    return (
        f"- 핵심전략(primary): {p.get('desc', '').strip()}\n"
        f"- 보조전략(secondary): {sec.get('desc', '').strip()}\n"
        f"- 체급 sweet spot: {ts.get('sweet_low')}~{ts.get('sweet_high')}원\n"
        f"- 낙찰방식 stance: {stance_str}"
    )


def _announcement_line(i: int, ann: Announcement, matched: list[str]) -> str:
    return (
        f"[{i}] {ann.title} | 사업유형:{ann.work_type or '?'}"
        f" | 수요기관:{ann.demand_inst or ann.notice_inst or '미상'}"
        f" | 추정가:{ann.estimated_price or '미상'} | 낙찰방식:{ann.award_method or '미상'}"
        f" | 실적경쟁:{ann.result_competition or '?'} | 기술평가율:{ann.tech_eval_rate or '-'}"
        f" | 가격평가율:{ann.price_eval_rate or '-'} | 재공고:{ann.re_notice or '?'}"
        f" | 정보화:{ann.info_biz or '?'} | 트리아지매칭:{','.join(matched) or '없음'}"
    )


def _knowledge_context(knowledge) -> list[str]:
    """평가·심층평가 프롬프트가 공유하는 회사 역량·자격·전략·평가기준 블록."""
    return [
        "# 우리 보유 자산(재사용 가능 역량)",
        _assets_block(knowledge),
        "",
        "# 자격·실적 상태",
        _eligibility_block(knowledge),
        "",
        "# 전략",
        _strategy_block(knowledge),
        "",
        *_CRITERIA,
    ]


def _build_prompt(knowledge, batch: list[tuple[Announcement, list[str]]]) -> str:
    lines = [
        "당신은 팀모노리스(에듀테크)의 공공조달 사업개발 담당자다.",
        "아래 회사 역량·자격·전략을 기준으로 각 입찰공고를 4축(0~100)으로 평가하라.",
        "",
        *_knowledge_context(knowledge),
        "",
        "# 평가 대상",
    ]
    for i, (ann, matched) in enumerate(batch):
        lines.append(_announcement_line(i, ann, matched))
    lines.append("")
    lines.append(
        "각 공고마다 index, axes(reuse/winnability/value/performance_building), quant_barrier, wired_risk, matched_assets, rationale 를 채워라."
    )
    return "\n".join(lines)


def evaluate(
    candidates: list[tuple[Announcement, list[str]]],
    knowledge,
    model: str,
    batch_size: int,
    llm=None,
) -> dict[int, EvalOut]:
    """후보(공고, 트리아지매칭) 리스트를 배치 평가 → {전역index: EvalOut}."""
    if not candidates:
        return {}
    client = llm or ChatOpenAI(model=model, temperature=0).with_structured_output(
        BatchEval
    )

    results: dict[int, EvalOut] = {}
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        out: BatchEval = client.invoke(_build_prompt(knowledge, batch))
        for ev in out.evaluations:
            if 0 <= ev.index < len(batch):
                results[start + ev.index] = ev
    return results


def _build_deep_prompt(
    knowledge, ann: Announcement, matched: list[str], spec_text: str
) -> str:
    lines = [
        "당신은 팀모노리스(에듀테크)의 공공조달 사업개발 담당자다.",
        "아래 회사 역량·자격·전략과 '규격서 본문'을 근거로 이 공고를 4축(0~100)으로 평가하라.",
        "규격서의 실제 과업범위·요구사항·평가배점·실적/지역요건을 우선 근거로 삼아라.",
        "",
        *_knowledge_context(knowledge),
        "",
        "# 공고 메타",
        _announcement_line(0, ann, matched),
        "",
        "# 규격서 본문(발췌)",
        spec_text,
        "",
        "규격서에서 '특정 제품·특정 업체 실적·특정 인증·기존 시스템 동일벤더 확약서'를 콕 집는 독소조항이 실제로 있을 때만 wired_risk를 med/high로. 자격 조건이 지나치게 많아 소수 업체만 통과 가능하거나, 정량 평가 배점이 지나치게 구체적이어서 특정 업체에 유리하게 설계된 경우도 med/high. 협상계약·고도화라는 사실만으로는 올리지 말 것(근거 없으면 low).",
        "index=0 으로 axes, quant_barrier, wired_risk, matched_assets, rationale 를 채워라. rationale 엔 규격서의 실제 과업 근거와 (있다면) lock-in 정황을 명시.",
    ]
    return "\n".join(lines)


def evaluate_deep(
    ann: Announcement,
    matched: list[str],
    spec_text: str,
    knowledge,
    model: str,
    llm=None,
) -> EvalOut:
    """규격서 본문까지 읽고 1건을 심층 재평가."""
    client = llm or ChatOpenAI(model=model, temperature=0).with_structured_output(
        EvalOut
    )
    return client.invoke(_build_deep_prompt(knowledge, ann, matched, spec_text))
