"""
처리되지 않은 ops@ 받은 메일을 다시 처리합니다.

웹훅은 SNS 에 먼저 200 을 돌려주고 뒤에서 처리합니다. 그 처리가 슬랙·DB 오류로
실패하면 SNS 는 다시 보내지 않아서, 메일은 S3 에만 남고 슬랙에도 DB 에도 없습니다.
이 스크립트가 S3 를 훑어 DB 에 기록이 없는 원본을 같은 함수로 다시 처리합니다.

방금 들어온 메일은 웹훅이 처리 중일 수 있어 건너뜁니다. 둘이 겹치면 같은
메일이 슬랙에 두 번 올라갑니다.

사용:
  python scripts/reconcile_ops_mail.py --dry-run
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from api.s3 import list_objects
from app.ops_mail import run_store
from app.ops_mail_sns import process_object
from service.ops_mail import store

load_dotenv()

# SES 수신 규칙이 원본을 넣는 prefix. ped-terraform 의 ops_mail_object_prefix 와 같다.
PREFIX = "ops/"

# 웹훅이 처리할 시간. 이보다 최근 객체는 건너뛴다.
GRACE = timedelta(minutes=5)


async def reconcile(dry_run: bool) -> None:
    """DB 에 없는 원본을 다시 처리합니다.

    Args:
        dry_run: True 면 처리할 키만 출력합니다
    """
    bucket = os.environ["OPS_MAIL_S3_BUCKET"]
    cutoff = datetime.now(timezone.utc) - GRACE
    keys = [
        item["Key"]
        for item in await asyncio.to_thread(list_objects, bucket, PREFIX)
        if item["LastModified"] < cutoff
    ]
    processed = await asyncio.to_thread(run_store, store.processed_keys, keys)
    missing = [key for key in keys if key not in processed]
    print(f"ops 메일 원본 {len(keys)}개 중 미처리 {len(missing)}개")

    for key in missing:
        if dry_run:
            print(f"[dry-run] 처리할 원본 {key}")
            continue
        await process_object(bucket, key)


def main(dry_run: bool = False) -> None:
    """스케줄러가 작업 스레드에서 부릅니다. 그 스레드에는 이벤트 루프가 없습니다."""
    asyncio.run(reconcile(dry_run))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="처리할 원본 키만 출력")
    main(dry_run=parser.parse_args().dry_run)
