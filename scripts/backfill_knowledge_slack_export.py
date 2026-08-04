"""
워크스페이스 Export를 지식베이스에 백필합니다.

Socket Mode 수집은 채널을 등록한 뒤의 메시지만 받습니다. 그 이전 대화는
Export로 채웁니다. 비마켓플레이스 앱은 conversations.history의 rate limit이
분당 1회라 API로는 과거를 훑을 수 없습니다.

Export의 일별 파일을 채널 단위로 모아 thread_ts로 묶고, Socket Mode와 같은
build_thread_row·upsert_thread를 거칩니다. 이메일 정규화도, 봇 단독 스레드를
skipped로 두는 판정도 같은 함수가 합니다.

content_hash가 같으면 정제 상태를 건드리지 않으므로 여러 번 돌려도, 실시간
수집과 겹쳐 돌려도 안전합니다.

사용법:
    python scripts/backfill_knowledge_slack_export.py --export-root ~/repo/slack-export/raw --dry-run
    python scripts/backfill_knowledge_slack_export.py --export-root ~/repo/slack-export/raw --channel t_개발
    python scripts/backfill_knowledge_slack_export.py --export-root ~/repo/slack-export/raw
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import pathlib
from typing import Any

import psycopg
from dotenv import load_dotenv

from app.knowledge import (
    DISTILL_DELAY_SECONDS,
    IGNORED_SUBTYPES,
    SLACK_WORKSPACE_DOMAIN,
)
from service.knowledge.db import connect, fetch_all
from service.knowledge.ingest import build_thread_row, upsert_thread
from service.knowledge.users import load_from_export

ENABLED_SOURCES = """
SELECT id, external_id, name, backfilled_at
FROM data_source
WHERE source = 'slack' AND enabled
ORDER BY name
"""

MARK_BACKFILLED = "UPDATE data_source SET backfilled_at = now() WHERE id = %s"


def load_export_names(export_root: pathlib.Path) -> dict[str, str]:
    """채널 ID를 Export 디렉터리 이름으로 바꾸는 매핑을 만듭니다.

    디렉터리 이름은 Export 시점의 채널 이름입니다. data_source.name은 등록
    시점의 이름이라 채널을 그 사이에 바꿨으면 서로 다릅니다. 변하지 않는
    채널 ID를 거쳐 찾습니다.

    Args:
        export_root: Export 압축을 푼 디렉터리

    Returns:
        dict[str, str]: 채널 ID → 디렉터리 이름
    """
    channels = json.loads((export_root / "channels.json").read_text(encoding="utf-8"))
    return {channel["id"]: channel["name"] for channel in channels}


def read_channel_messages(channel_dir: pathlib.Path) -> list[dict[str, Any]]:
    """채널의 일별 파일을 전부 읽어 메시지를 모읍니다.

    스레드는 날짜를 넘나듭니다. 부모가 3월 파일에 있고 답글이 7월 파일에
    있는 경우가 흔하므로 채널 전체를 한 번에 읽습니다.

    Args:
        channel_dir: 채널 하나의 Export 디렉터리

    Returns:
        list[dict[str, Any]]: 파일 순서대로 이어붙인 메시지 목록
    """
    messages: list[dict[str, Any]] = []
    for path in sorted(channel_dir.glob("*.json")):
        messages.extend(json.loads(path.read_text(encoding="utf-8")))
    return messages


def group_threads(
    messages: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], int]:
    """메시지를 스레드로 묶어 conversations.replies와 같은 형태로 만듭니다.

    부모가 Export 범위 밖에 있는 스레드는 버립니다. build_thread_row가 첫
    메시지를 부모로 보고 external_id를 만들기 때문에, 답글만 넣으면 실시간
    수집이 만드는 행과 external_id가 달라져 같은 스레드가 두 행이 됩니다.

    Args:
        messages: 채널의 전체 메시지 목록

    Returns:
        tuple: (스레드별 메시지 목록, 부모가 없어 버린 스레드 수)
    """
    threads: dict[str, dict[str, dict[str, Any]]] = {}
    for message in messages:
        if message.get("subtype") in IGNORED_SUBTYPES:
            continue
        thread_ts = message.get("thread_ts") or message["ts"]
        # 같은 메시지가 여러 날짜 파일에 나오면 한 번만 담는다.
        threads.setdefault(thread_ts, {})[message["ts"]] = message

    complete = [
        [by_ts[ts] for ts in sorted(by_ts, key=float)]
        for thread_ts, by_ts in threads.items()
        if thread_ts in by_ts
    ]
    return complete, len(threads) - len(complete)


def backfill_channel(
    conn: psycopg.Connection,
    data_source_id: int,
    channel_id: str,
    threads: list[list[dict[str, Any]]],
    user_emails: dict[str, str],
) -> int:
    """채널 하나의 스레드를 적재합니다.

    Args:
        conn: 커넥션
        data_source_id: data_source.id
        channel_id: Slack 채널 ID
        threads: group_threads 결과
        user_emails: Slack UID → 이메일 매핑

    Returns:
        int: 새로 삽입된 행 수. 나머지는 기존 행 갱신이다
    """
    inserted = 0
    for messages in threads:
        row = build_thread_row(
            data_source_id=data_source_id,
            channel_id=channel_id,
            messages=messages,
            workspace_domain=SLACK_WORKSPACE_DOMAIN,
            distill_delay_seconds=DISTILL_DELAY_SECONDS,
            user_emails=user_emails,
        )
        inserted += upsert_thread(conn, row)["inserted"]
    return inserted


def backfill(
    export_root: pathlib.Path,
    channel_filter: str | None,
    dsn: str | None,
    dry_run: bool,
) -> None:
    """등록된 채널을 Export로 백필합니다.

    채널 하나가 끝날 때마다 커밋합니다. 물량이 커서 중간에 끊겨도 끝난
    채널은 남기기 위해서입니다.

    Args:
        export_root: Export 압축을 푼 디렉터리
        channel_filter: 이 이름의 채널만 처리합니다. None이면 전부
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL을 씁니다
        dry_run: True면 적재하지 않고 대상만 셉니다
    """
    user_emails = load_from_export(export_root)
    export_names = load_export_names(export_root)
    print(f"Export 사용자 {len(user_emails)}명, 채널 {len(export_names)}개")

    with connect(dsn) as conn:
        sources = fetch_all(conn, ENABLED_SOURCES)
        if channel_filter:
            sources = [s for s in sources if s["name"] == channel_filter]
        print(f"수집 대상 채널 {len(sources)}개\n")

        for source in sources:
            export_name = export_names.get(source["external_id"])
            channel_dir = export_root / export_name if export_name else None
            if channel_dir is None or not channel_dir.is_dir():
                print(f"{source['name']:<30} Export에 없음, 건너뜀")
                continue

            threads, orphans = group_threads(read_channel_messages(channel_dir))
            done = "" if source["backfilled_at"] is None else " (재실행)"
            note = f", 부모 없어 버림 {orphans}" if orphans else ""

            if dry_run:
                print(f"{source['name']:<30} 스레드 {len(threads):>6}{note}{done}")
                continue

            inserted = backfill_channel(
                conn, source["id"], source["external_id"], threads, user_emails
            )
            with conn.cursor() as cur:
                cur.execute(MARK_BACKFILLED, (source["id"],))
            conn.commit()
            print(
                f"{source['name']:<30} 스레드 {len(threads):>6}"
                f" 신규 {inserted:>6} 갱신 {len(threads) - inserted:>6}{note}"
            )


def main() -> None:
    """명령행 인자를 파싱해 백필을 실행합니다."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="Slack Export 지식베이스 백필")
    parser.add_argument(
        "--export-root",
        required=True,
        type=pathlib.Path,
        help="Export 압축을 푼 디렉터리 (users.json·channels.json이 있는 곳)",
    )
    parser.add_argument("--channel", help="이 이름의 채널만 처리")
    parser.add_argument("--dsn", help="접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL")
    parser.add_argument(
        "--dry-run", action="store_true", help="적재하지 않고 대상만 셈"
    )
    args = parser.parse_args()

    backfill(args.export_root.expanduser(), args.channel, args.dsn, args.dry_run)


if __name__ == "__main__":
    main()
