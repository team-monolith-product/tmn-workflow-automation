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
from datetime import date

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import load_config
from service.edu_bid import pipeline, stages
from service.edu_bid.knowledge import load_knowledge

load_dotenv()


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
    args = parser.parse_args()

    cfg = load_config().education_bid_crawler
    if not cfg:
        print("education_bid_crawler 설정이 config.yaml에 없습니다.")
        return

    knowledge = load_knowledge()
    decisions = pipeline.run(
        model=cfg.model,
        lookback_days=cfg.lookback_days,
        batch_size=cfg.batch_size,
        today=date.today(),
        dry_run=args.dry_run,
        limit=args.limit,
        do_enrich=not args.no_enrich,
        knowledge=knowledge,
    )

    window = stages.build_window(date.today(), cfg.lookback_days)
    text = stages.format_report(decisions, window)
    reportable = [d for d in decisions if d.label in ("입찰추천", "검토", "미래타깃")]
    if not reportable:
        print("[edu-bid] 보고 대상 없음. Slack 전송 생략.")
        return

    if args.dry_run:
        print("----- DRY RUN: Slack 메시지 -----")
        print(text)
        return

    WebClient(token=os.environ["SLACK_BOT_TOKEN"]).chat_postMessage(
        channel=cfg.channel_id, text=text
    )
    print(f"[edu-bid] Slack 전송 완료 → {cfg.channel_id}")


if __name__ == "__main__":
    main()
