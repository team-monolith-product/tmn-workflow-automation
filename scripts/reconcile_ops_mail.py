"""
처리되지 않은 ops@ 받은 메일을 다시 처리합니다.

웹훅은 SNS 에 먼저 200 을 돌려주고 뒤에서 처리합니다. 서버가 웹훅을 못 받았거나
처리가 슬랙·DB 오류로 실패하면 SNS 는 다시 보내지 않습니다. 이 스크립트가 S3 를
훑어 처리가 끝나지 않은 원본을 같은 함수로 다시 처리합니다.

같은 원본을 웹훅과 이 스크립트가 함께 집어도 슬랙에는 한 번만 올라갑니다.
process_object 가 올리기 전에 DB 에 자리를 잡고, 맡은 지 얼마 안 된 메일은
다른 쪽이 건너뜁니다. 몇 번 넘게 실패한 원본은 더 다시 하지 않습니다.

오래된 원본은 보지 않습니다. 하루 넘게 실패하는 원본은 다시 해도 같은 이유로
실패하니 사람이 봐야 하고, 모든 키를 DB 에 물으면 메일이 쌓일수록 무거워집니다.

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
from app.ops_mail_sns import process_object
from service.ops_mail import store

load_dotenv()

# SES 수신 규칙이 원본을 넣는 prefix. ped-terraform 의 ops_mail_object_prefix 와 같다.
PREFIX = "ops/"

# 웹훅이 처리할 시간. 이보다 최근 객체는 건너뛴다.
GRACE = timedelta(minutes=5)

# 이보다 오래된 원본은 다시 처리하지 않는다.
LOOKBACK = timedelta(days=1)


async def reconcile(dry_run: bool) -> None:
    """처리가 끝나지 않은 원본을 다시 처리합니다.

    Args:
        dry_run: True 면 처리할 키만 출력합니다
    """
    bucket = os.environ["OPS_MAIL_S3_BUCKET"]
    now = datetime.now(timezone.utc)
    keys = [
        item["Key"]
        for item in await asyncio.to_thread(list_objects, bucket, PREFIX)
        if now - LOOKBACK < item["LastModified"] < now - GRACE
    ]
    # 끝났거나 횟수를 다 쓴 것은 뺀다. 남은 것을 맡을지는 process_object 가 정한다
    # -- 다른 쪽이 처리 중이면 거기서 건너뛴다.
    settled = await store.run(store.settled_keys, keys)
    missing = [key for key in keys if key not in settled]
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
