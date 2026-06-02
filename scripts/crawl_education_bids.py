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
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import load_config
from service.edu_bid import pipeline, stages
from service.edu_bid.knowledge import load_knowledge
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

    # 공유 상단부는 트랙 무관 — 아무 트랙 지식으로 한 번만 수집·게이트한다.
    shared_knowledge = load_knowledge(cfg.tracks[0].key)
    prep = pipeline.prepare(
        window,
        shared_knowledge,
        limit=args.limit,
        use_cache=not args.no_cache,
    )

    client = None if args.dry_run else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    per_track: dict[str, list] = {}
    for track in cfg.tracks:
        knowledge = load_knowledge(track.key)
        decisions = pipeline.run_track(
            track.name,
            track.work_types,
            prep.gated,
            knowledge,
            cfg.model,
            cfg.batch_size,
            do_enrich=not args.no_enrich,
        )
        per_track[track.key] = decisions  # 제외건 포함 — DB 적재·디버그용

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

    # DB 적재 — enk Postgres. DATABASE_URL 미설정이면 건너뜀(enk 연결 전 단계 호환).
    # dry-run 은 부수효과 없이 두기 위해 적재하지 않는다.
    if args.dry_run:
        return
    if not os.environ.get("DATABASE_URL"):
        print("[edu-bid] DATABASE_URL 없음 — DB 적재 생략")
        return
    from service.edu_bid import db, store

    with db.session_scope() as session:
        run_id = store.persist_run(session, window, prep, per_track)
    total = sum(len(v) for v in per_track.values())
    print(f"[edu-bid] DB 적재 완료 run_id={run_id} (결정 {total}건)")


if __name__ == "__main__":
    main()
