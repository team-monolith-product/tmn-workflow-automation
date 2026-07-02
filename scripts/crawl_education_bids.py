"""
나라장터(G2B) 교육 외주 입찰공고 수집·평가 (단계형 파이프라인 오케스트레이터)

단계 구현은 service/edu_bid/ 에, 변동 지식은 knowledge/edu_bid/ 에 있다.
이 스크립트는 설정을 읽어 파이프라인을 호출하고 결과를 Slack 으로 보고하는 얇은 진입점.

독립 실행:
    python scripts/crawl_education_bids.py            # 실제 Slack 전송
    python scripts/crawl_education_bids.py --dry-run  # 전송 없이 콘솔 출력
    python scripts/crawl_education_bids.py --dry-run --limit 40  # 평가 상한(테스트)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import load_config
from service.edu_bid import pipeline, stages
from service.edu_bid.knowledge import load_knowledge, load_shared_knowledge
from service.edu_bid.schemas import REPORTABLE_LABELS

load_dotenv()

KST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser(
        description="나라장터 교육 외주 입찰공고 수집·평가"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Slack 전송 없이 콘솔 출력"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="평가할 공고 수 상한(테스트용)"
    )
    parser.add_argument(
        "--no-enrich", action="store_true", help="S4 규격서 정독 건너뜀"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="원본 캐시 무시하고 API 재호출"
    )
    args = parser.parse_args()

    cfg = load_config().education_bid_crawler
    if not cfg:
        print("education_bid_crawler 설정이 config.yaml에 없습니다.")
        return

    # 어제 같은 시각 ~ 현재(무상태). 매일 같은 시각 실행이라 구간이 빈틈·중복 없이 이어진다.
    now = datetime.now(KST)
    window = stages.build_incremental_window(now)

    # 공유 상단부는 트랙 무관 — 공유 지식을 run 당 한 번만 만들어 게이트·트랙 루프가 함께 쓴다.
    shared = load_shared_knowledge()
    gated = pipeline.prepare(
        window,
        shared,
        limit=args.limit,
        use_cache=not args.no_cache,
    )

    # 어느 트랙에도 매핑되지 않은 사업유형(연구·기타 등)은 평가 없이 누락된다 — 침묵 스킵 방지로 집계.
    routed_types = {wt for t in cfg.tracks for wt in t.work_types}
    unrouted = Counter(
        a.work_type for a, _, _ in gated if a.work_type not in routed_types
    )
    if unrouted:
        print(
            f"[edu-bid] 트랙 미매핑 제외: {sum(unrouted.values())}건 {dict(unrouted)}"
        )

    client = None if args.dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    for track in cfg.tracks:
        knowledge = load_knowledge(track.key, shared=shared)
        decisions = pipeline.run_track(
            track.name,
            track.work_types,
            gated,
            knowledge,
            cfg.model,
            cfg.batch_size,
            do_enrich=not args.no_enrich,
        )
        reportable = [d for d in decisions if d.label in REPORTABLE_LABELS]
        if not reportable:
            print(f"[edu-bid][{track.name}] 보고 대상 없음. Slack 전송 생략.")
            continue

        text = stages.format_report(decisions, window, track.name)
        if args.dry_run:
            print(f"----- DRY RUN: [{track.name}] Slack 메시지 -----")
            print(text)
            continue

        client.chat_postMessage(channel=track.channel_id, text=text)
        print(f"[edu-bid][{track.name}] Slack 전송 완료 → {track.channel_id}")


if __name__ == "__main__":
    main()
