"""
정제 대기 중인 지식베이스 스레드를 LLM으로 재작성합니다.

스케줄러가 주기적으로 부르고, 회차마다 max_per_run 건씩만 처리합니다. Export
백필로 들어온 1.4만 건이 distill_after를 전부 지나 있어 상한이 없으면 첫
회차에 통째로 대상이 됩니다. 며칠에 걸쳐 흘려보내면 초기 결과를 보고 프롬프트를
고칠 여지가 남습니다.

LLM 호출만 병렬로 돌리고 DB 쓰기는 순차입니다. 커넥션 하나를 여러 스레드가
같이 쓰면 안 됩니다.

사용법:
    python scripts/distill_knowledge_items.py --dry-run --limit 5
    python scripts/distill_knowledge_items.py --limit 5
    python scripts/distill_knowledge_items.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

from service.config import load_config
from service.knowledge.db import connect
from service.knowledge.distill import (
    Distilled,
    acquire_lock,
    build_client,
    count_pending,
    distill_thread,
    fetch_pending,
    mark_error,
    render_distilled_text,
    store_distilled,
)


def distill_batch(limit: int, concurrency: int, dsn: str | None, dry_run: bool) -> None:
    """정제 대기 중인 스레드를 한 회차 처리합니다.

    한 건이 실패해도 나머지를 계속합니다. 실패한 건은 error로 옮겨 다음
    회차에 다시 잡히지 않게 합니다. 그러지 않으면 통과하지 못하는 스레드
    하나가 큐 앞을 계속 막습니다.

    Args:
        limit: 이번 회차에 처리할 최대 건수
        concurrency: 동시에 돌릴 LLM 호출 수
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL을 씁니다
        dry_run: True면 저장하지 않고 결과를 출력합니다
    """
    with connect(dsn) as conn:
        if not acquire_lock(conn):
            print("다른 회차가 도는 중입니다. 건너뜁니다.")
            return

        total = count_pending(conn)
        items = fetch_pending(conn, limit)
        # LLM 호출 전에 트랜잭션을 닫는다. 안 닫으면 회차 내내(수 분) idle in
        # transaction 으로 남아 item 테이블의 vacuum 을 그만큼 막는다.
        conn.commit()
        print(f"정제 대기 {total}건 중 {len(items)}건 처리")

        if not items:
            return

        client = build_client()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            outcomes = list(
                executor.map(lambda item: _distill_one(item, client), items)
            )

        if len(items) > 1 and all(error is not None for _, _, error in outcomes):
            # 전부 실패면 개별 스레드 문제가 아니라 프롬프트·스키마·키 사고다.
            # 그대로 error 를 찍으면 회차마다 limit 건씩 영구 제외되고, 이틀이면
            # 큐 전체가 굳는다. 아무것도 옮기지 않고 시끄럽게 죽는다.
            raise RuntimeError(
                f"{len(items)}건이 전부 실패했습니다. 첫 사유: {outcomes[0][2]}"
            )

        stored = skipped = failed = 0
        for item, distilled, error in outcomes:
            if error is not None:
                failed += 1
                print(f"  #{item['id']} 실패: {error}")
                if not dry_run:
                    state = mark_error(conn, item["id"], error)
                    conn.commit()
                    if state == "error":
                        print(f"  #{item['id']} 재시도 한도 초과 — error 로 옮김")
                continue

            if dry_run:
                print(f"  #{item['id']}\n{render_distilled_text(distilled)}\n")
                continue

            if store_distilled(conn, item["id"], item["content_hash"], distilled):
                stored += 1
            else:
                # 정제하는 사이에 답글이 달렸다. 새 내용으로 다시 잡힌다.
                skipped += 1
            conn.commit()

        if dry_run:
            print(f"드라이런: 저장하지 않음 (실패 {failed})")
        else:
            print(f"저장 {stored}, 내용 변경으로 건너뜀 {skipped}, 실패 {failed}")


def _distill_one(item: dict, client) -> tuple[dict, Distilled | None, str | None]:
    """스레드 한 건을 정제합니다. 실패는 사유 문자열로 돌려줍니다.

    Args:
        item: fetch_pending이 돌려준 행
        client: 회차가 공유하는 LLM 클라이언트

    Returns:
        tuple: (원본 행, 정제 결과 또는 None, 실패 사유 또는 None)
    """
    try:
        return item, distill_thread(item["raw_text"], client), None
    except Exception as exc:
        return item, None, f"{type(exc).__name__}: {exc}"


def main() -> None:
    """명령행 인자를 파싱해 한 회차를 실행합니다."""
    load_dotenv()
    config = load_config().knowledge_distill

    parser = argparse.ArgumentParser(description="지식베이스 스레드 정제")
    parser.add_argument(
        "--limit",
        type=int,
        default=config.max_per_run,
        help=f"이번 회차 처리 건수 (기본 {config.max_per_run})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=config.concurrency,
        help=f"동시 LLM 호출 수 (기본 {config.concurrency})",
    )
    parser.add_argument("--dsn", help="접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL")
    parser.add_argument(
        "--dry-run", action="store_true", help="저장하지 않고 결과만 출력"
    )
    args = parser.parse_args()

    distill_batch(args.limit, args.concurrency, args.dsn, args.dry_run)


if __name__ == "__main__":
    main()
