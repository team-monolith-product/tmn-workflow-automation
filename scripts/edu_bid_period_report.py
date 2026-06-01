"""
교육 외주 입찰 — 기간 종합 보고서 (풀 파이프라인, LLM 정독 포함)

지난 N 영업일 각각에 대해 전체 파이프라인(수집→트리아지→사업유형→게이트→
LLM 4축 평가→S4 규격서 정독→결정)을 돌려, 추천/검토/미래타깃을 한 보고서로 종합한다.
원본 API 는 날짜·소스 캐시를 사용하지만 LLM 평가 비용은 발생한다.

    python scripts/edu_bid_period_report.py --business-days 15
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import traceback
from collections import Counter
from datetime import date, timedelta

from dotenv import load_dotenv

from service.config import load_config
from service.edu_bid import pipeline
from service.edu_bid.knowledge import load_knowledge
from service.edu_bid.stages import format_won as _won
from service.edu_bid.schemas import REPORTABLE_LABELS as _REPORT_LABELS

load_dotenv()

_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def business_days(n: int) -> list[date]:
    out: list[date] = []
    d = date.today() - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="교육 외주 입찰 기간 종합 보고서")
    ap.add_argument("--business-days", type=int, default=15, help="과거 영업일 수")
    args = ap.parse_args()

    cfg = load_config().education_bid_crawler
    kn = load_knowledge()
    days = business_days(args.business_days)
    print(f"[period] 분석 영업일 {len(days)}일: {days[0]}~{days[-1]}")

    collected: list[tuple[date, object]] = []  # (게시일, Decision)
    for d in days:
        print(f"\n===== {d} ({_WEEKDAY[d.weekday()]}) =====")
        try:
            decisions = pipeline.run(
                model=cfg.model,
                lookback_days=1,
                batch_size=cfg.batch_size,
                today=d + timedelta(days=1),  # window=[d]
                do_enrich=True,
                use_cache=True,
                knowledge=kn,
            )
        except Exception:
            print(f"[period] {d} 실패:\n{traceback.format_exc()}")
            continue
        for dec in decisions:
            if dec.label in _REPORT_LABELS:
                collected.append((d, dec))

    # ── 본공고 / 사전규격 분리 ──
    notice = [(d, x) for d, x in collected if x.announcement.stage != "presearch"]
    presearch = [(d, x) for d, x in collected if x.announcement.stage == "presearch"]

    L: list[str] = []

    def detail(d, dec, deadline_label, deadline_val):
        a = dec.announcement
        L.append(
            f"### [{dec.score}점{' · 정독' if dec.enriched else ''}] {a.title} ({d})"
        )
        L.append("")
        L.append(
            f"- 사업유형: {a.work_type} | 수요기관: {a.demand_inst or a.notice_inst or '미상'} | 예산/추정: {_won(a.estimated_price)} | {deadline_label}: {deadline_val or '미상'}"
        )
        L.append(
            f"- 축: 재사용 {dec.axes.get('reuse')} / 수주 {dec.axes.get('winnability')} / 가치 {dec.axes.get('value')} / 실적적립 {dec.axes.get('performance_building')} | 정량장벽 {dec.quant_barrier} | 내정위험 {dec.wired_risk}"
        )
        L.append(
            f"- 매칭 자산: {', '.join(dec.matched_assets) if dec.matched_assets else '-'}"
        )
        L.append(f"- 근거: {dec.rationale}")
        if dec.gate.reasons:
            L.append(f"- 게이트: {'; '.join(dec.gate.reasons)}")
        L.append(f"- 링크: {a.url}")
        L.append("")

    L.append(
        f"# 교육 외주 입찰 종합 보고서 — {days[0]} ~ {days[-1]} (영업일 {len(days)})"
    )
    L.append("")
    L.append(
        "- 소스: 나라장터 용역 · 풀 파이프라인(LLM 정독 포함) · dry-run · 본공고/사전규격 분리"
    )
    L.append("")

    # ===== Part 1: 본공고 =====
    nby = {
        lab: sorted(
            [(d, x) for d, x in notice if x.label == lab], key=lambda t: -t[1].score
        )
        for lab in _REPORT_LABELS
    }
    n_wt = Counter(
        x.announcement.work_type for _, x in notice if x.label in _REPORT_LABELS
    )
    L.append("# 1. 본공고")
    L.append("")
    L.append(
        f"- 보고 대상: 입찰추천 {len(nby['입찰추천'])} · 검토 {len(nby['검토'])} · 미래타깃 {len(nby['미래타깃'])}"
    )
    L.append(f"- 사업유형 분포: {dict(n_wt.most_common())}")
    L.append("")

    def notice_table(rows):
        L.append(
            "| 게시일 | 점수 | 정독 | 사업유형 | 공고명 | 수요기관 | 추정가격 | 정량장벽 | 내정위험 | 낙찰 | 링크 |"
        )
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for d, dec in rows:
            a = dec.announcement
            L.append(
                f"| {d.strftime('%m-%d')} | {dec.score} | {'O' if dec.enriched else ''} "
                f"| {a.work_type} | {a.title.replace('|','/')} "
                f"| {(a.demand_inst or a.notice_inst or '미상').replace('|','/')} "
                f"| {_won(a.estimated_price)} | {dec.quant_barrier} | {dec.wired_risk} "
                f"| {(a.award_method or '미상').replace('|','/')} | {a.url} |"
            )

    for lab in _REPORT_LABELS:
        L.append(f"## [본공고] {lab} ({len(nby[lab])}건)")
        L.append("")
        if nby[lab]:
            notice_table(nby[lab])
        else:
            L.append("_해당 없음_")
        L.append("")
    L.append("## [본공고] 입찰추천 상세")
    L.append("")
    for d, dec in nby["입찰추천"]:
        detail(d, dec, "마감", dec.announcement.close_dt)

    # ===== Part 2: 사전규격 — 가능성 있는 건 =====
    promising = [(d, x) for d, x in presearch if x.label in ("입찰추천", "검토")]
    # 의견마감 임박순(빈 값은 뒤로) → 점수
    promising.sort(
        key=lambda t: (t[1].announcement.opinion_close_dt or "9999", -t[1].score)
    )
    p_wt = Counter(x.announcement.work_type for _, x in promising)
    L.append(f"# 2. 사전규격 — 가능성 있는 건 ({len(promising)})")
    L.append("")
    L.append(
        "사전규격은 본공고 전 단계. 의견마감 전이면 규격 의견제출(영업) 윈도우가 열려 있어, 우리 자산에 맞게 규격을 유도할 기회."
    )
    L.append(f"- 사업유형 분포: {dict(p_wt.most_common())}")
    L.append("")
    L.append(
        "| 의견마감 | 점수 | 정독 | 사업유형 | 사업명 | 수요기관 | 예산 | 규격파일 | 링크 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|")
    for d, dec in promising:
        a = dec.announcement
        L.append(
            f"| {a.opinion_close_dt or '미상'} | {dec.score} | {'O' if dec.enriched else ''} "
            f"| {a.work_type} | {a.title.replace('|','/')} "
            f"| {(a.demand_inst or a.notice_inst or '미상').replace('|','/')} "
            f"| {_won(a.estimated_price)} | {len(a.spec_docs)} | {a.url} |"
        )
    L.append("")
    L.append("## [사전규격] 입찰추천 상세")
    L.append("")
    for d, dec in [t for t in promising if t[1].label == "입찰추천"]:
        detail(d, dec, "의견마감", dec.announcement.opinion_close_dt)

    out = Path(__file__).parent.parent.parent / "edu-bid-period-report.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n[written] {out}")
    print(
        f"[period] 본공고 추천 {len(nby['입찰추천'])}·검토 {len(nby['검토'])} | "
        f"사전규격 가능성 {len(promising)}건"
    )


if __name__ == "__main__":
    main()
