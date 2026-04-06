"""
매월 1일 배포 담당자 스케줄 공지

해당 월의 요일별 배포 담당자를 계산하여 공지-배포 채널에 안내 메시지를 전송합니다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
from datetime import date

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import load_config
from service.deployment_rotation import get_weekday_schedule, WEEKDAY_NAMES

load_dotenv()


def main():
    config = load_config()
    rotation = config.deployment_rotation
    if not rotation:
        print("deployment_rotation 설정이 없습니다.")
        return

    today = date.today()
    year, month = today.year, today.month
    schedule = get_weekday_schedule(year, month, rotation.members)

    lines = [f"\U0001f4cb {month}월 배포 담당자 안내\n"]
    for weekday_idx in range(5):
        user_id = schedule[weekday_idx]
        lines.append(f"{WEEKDAY_NAMES[weekday_idx]}: <@{user_id}>")

    message = "\n".join(lines)

    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    slack_client.chat_postMessage(channel=rotation.channel_id, text=message)
    print(f"{year}년 {month}월 배포 담당자 공지 전송 완료.")


if __name__ == "__main__":
    main()
