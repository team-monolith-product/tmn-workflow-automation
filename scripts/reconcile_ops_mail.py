"""
처리되지 않은 ops@ 받은 메일을 다시 처리합니다.

웹훅은 SNS 에 먼저 200 을 돌려주고 뒤에서 처리합니다. 서버가 웹훅을 못 받았거나
처리가 슬랙·DB 오류로 실패하면 SNS 는 다시 보내지 않습니다. 이 스크립트가 S3 를
훑어 처리가 끝나지 않은 원본을 같은 함수로 다시 처리합니다.

같은 원본을 웹훅과 이 스크립트가 함께 집어도 동시에 둘이 올리지 않습니다.
process_object 가 원본을 읽기 전에 DB 에 자리를 잡고, 맡은 지 얼마 안 된 원본은
다른 쪽이 건너뜁니다. 몇 번 넘게 실패한 원본은 더 다시 하지 않고 이 스크립트가
채널에 한 번 알립니다.

일주일보다 오래된 원본은 보지 않습니다. 모든 키를 DB 에 물으면 메일이 쌓일수록
무거워집니다.

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

from slack_sdk.web.async_client import AsyncWebClient

from api.s3 import list_objects
from service.ops_mail import store
from service.ops_mail.inbound import format_given_up, process_object, ses_message_id

load_dotenv()

# SES 수신 규칙이 원본을 넣는 prefix. ped-terraform 의 ops_mail_object_prefix 와 같다.
PREFIX = "ops/"

# 웹훅이 처리할 시간. 이보다 최근 객체는 건너뛴다.
GRACE = timedelta(minutes=5)

# 이보다 오래된 원본은 보지 않는다. 웹훅과 정리 작업이 함께 며칠 내려가 있었어도
# (주말 장애 등) 그동안 들어온 메일을 놓치지 않게 넉넉히 둔다.
LOOKBACK = timedelta(days=7)


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
    settled = await asyncio.to_thread(
        store.settled, [ses_message_id(key) for key in keys]
    )
    missing = [key for key in keys if ses_message_id(key) not in settled]
    print(f"ops 메일 원본 {len(keys)}개 중 미처리 {len(missing)}개")

    for key in missing:
        if dry_run:
            print(f"[dry-run] 처리할 원본 {key}")
            continue
        # 하나가 실패해도 나머지는 처리한다. 던지면 그 뒤 원본이 매번 밀린다.
        try:
            await process_object(bucket, key)
        except Exception as error:
            print(f"ops 메일 재처리 실패 {key} {type(error).__name__}: {error}")

    # 횟수를 다 쓴 원본은 채널에 한 번 알린다. 안 알리면 메일이 조용히 사라진다.
    # 마지막 시도 중에 프로세스째 죽은 원본도 여기서 잡힌다. 알린 뒤에 표시해서,
    # 알리기가 실패하면 다음 번에 다시 알린다.
    given_up = await asyncio.to_thread(store.given_up)
    if dry_run:
        for row in given_up:
            print(f"[dry-run] 포기를 알릴 원본 {row['s3_key']} {row['last_error']}")
        return
    if given_up:
        client = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
        channel = os.environ["OPS_MAIL_SLACK_CHANNEL_ID"]
        for row in given_up:
            # 하나를 못 알려도 나머지는 알린다. 못 알린 것은 다음 번에 다시 알린다.
            try:
                await client.chat_postMessage(
                    channel=channel,
                    text=format_given_up(row, bucket),
                )
                await asyncio.to_thread(store.mark_alerted, row["ses_message_id"])
            except Exception as error:
                print(f"ops 메일 포기 알림 실패 {row['s3_key']} {type(error).__name__}")


def main(dry_run: bool = False) -> None:
    """스케줄러가 작업 스레드에서 부릅니다. 그 스레드에는 이벤트 루프가 없습니다."""
    asyncio.run(reconcile(dry_run))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="처리할 원본 키만 출력")
    main(dry_run=parser.parse_args().dry_run)
