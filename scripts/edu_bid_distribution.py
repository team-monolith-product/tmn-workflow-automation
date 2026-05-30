"""
교육 외주 입찰 — 과거 다일치 빈도·분포 분석 (룰 기반, LLM 미사용)

지난 N일에 대해 수집(캐시)→트리아지→사업유형 분류→게이트만 적용하여,
우리가 노려볼 수 있는(참가가능·유관 사업유형) 공고의 일별 빈도와 사업유형 분포를 집계한다.
LLM 평가는 하지 않으므로 비용이 들지 않는다(API는 날짜·소스 캐시 사용).

    python scripts/edu_bid_distribution.py --days 14
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from collections import Counter
from datetime import date, timedelta

from dotenv import load_dotenv

from service.edu_bid import sources, stages
from service.edu_bid.knowledge import load_knowledge

load_dotenv()

_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def analyze_day(day: date, kn, kw_index, drop_types) -> dict:
    bgn = day.strftime("%Y%m%d") + "0000"
    end = day.strftime("%Y%m%d") + "2359"
    anns = stages.dedupe_by_notice(sources.collect(kn, (bgn, end)))

    triaged = [a for a in anns if stages.triage(a, kw_index)]
    wt_counts: Counter = Counter()
    targetable = 0
    near_miss = 0
    for a in triaged:
        a.work_type = stages.classify_work_type(a, kn.work_types)
        if a.work_type in drop_types:
            continue
        g = stages.gate(a, kn.eligibility_ledger)
        if g.status == "fail":
            continue
        targetable += 1
        if g.status == "near_miss":
            near_miss += 1
        wt_counts[a.work_type] += 1
    return {
        "day": day,
        "collected": len(anns),
        "triaged": len(triaged),
        "targetable": targetable,
        "near_miss": near_miss,
        "wt": wt_counts,
    }


def main():
    parser = argparse.ArgumentParser(description="교육 외주 입찰 과거 빈도·분포 분석")
    parser.add_argument("--days", type=int, default=14, help="어제부터 과거 N일")
    args = parser.parse_args()

    kn = load_knowledge()
    kw_index = stages.build_keyword_index(kn.capability_profile)
    drop_types = set(kn.work_types.get("drop_for_eval", []))

    rows = [
        analyze_day(date.today() - timedelta(days=i), kn, kw_index, drop_types)
        for i in range(1, args.days + 1)
    ]

    total_wt: Counter = Counter()
    for r in rows:
        total_wt.update(r["wt"])
    wt_order = [wt for wt, _ in total_wt.most_common()]

    lines = [f"# 교육 외주 입찰 빈도·분포 — 최근 {args.days}일 (용역)", ""]
    lines.append("타깃 = 트리아지 통과 + 참가가능(수의시담 제외) + 유관 사업유형")
    lines.append("")
    header = ["날짜", "요일", "수집", "트리아지", "타깃", "미래(실적)"] + wt_order
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    biz_targ = []
    for r in rows:
        d = r["day"]
        dow = _WEEKDAY[d.weekday()]
        cells = [
            d.strftime("%m-%d"),
            dow,
            str(r["collected"]),
            str(r["triaged"]),
            str(r["targetable"]),
            str(r["near_miss"]),
        ] + [str(r["wt"].get(wt, 0)) for wt in wt_order]
        lines.append("| " + " | ".join(cells) + " |")
        if d.weekday() < 5:
            biz_targ.append(r["targetable"])

    lines.append("")
    n_biz = len(biz_targ)
    avg = sum(biz_targ) / n_biz if n_biz else 0
    lines.append("## 요약")
    lines.append("")
    lines.append(
        f"- 평일 {n_biz}일 평균 타깃: {avg:.1f}건/일 (최소 {min(biz_targ) if biz_targ else 0}, 최대 {max(biz_targ) if biz_targ else 0})"
    )
    lines.append(f"- 기간 총 타깃: {sum(r['targetable'] for r in rows)}건")
    lines.append("")
    lines.append("### 사업유형 분포 (타깃 기준, 전체 기간)")
    lines.append("")
    lines.append("| 사업유형 | 건수 | 비중 |")
    lines.append("|---|---|---|")
    grand = sum(total_wt.values()) or 1
    for wt, c in total_wt.most_common():
        lines.append(f"| {wt} | {c} | {c/grand*100:.1f}% |")

    out = Path(__file__).parent.parent.parent / "edu-bid-distribution.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
