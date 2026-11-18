"""
해커톤 리드 아침 정리 DM (문의지기 F6 + F7)

업무일 10:00 에 노션 「🗂 리드 파이프라인」에서 오늘 연락할 리드와 자료가 빠진 리드를
찾아 이주현 님께 DM 한 통을 보냅니다. 이주현 님이 휴가면 김세윤 님께 보냅니다.
오늘 연락할 리드는 그 문의 스레드에도 "오늘 연락 예정" 을 한 줄 남깁니다.
둘 다 해당이 없는 날은 보내지 않습니다.

독립 실행:
    python scripts/notify_hackathon_leads.py            # 실제 전송
    python scripts/notify_hackathon_leads.py --dry-run  # 전송 없이 콘솔 출력
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from datetime import datetime

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

from service.hackathon_inquiry import (
    BACKUP_USER_ID,
    CHANNEL_ID,
    KST,
    OWNER_USER_ID,
    SUMMARY_MARKER,
    is_vacation_profile,
)
from service.hackathon_leads import build_morning_report

load_dotenv()


def main(dry_run: bool = False):
    notion = NotionClient(
        auth=os.environ["NOTION_TOKEN"].strip(), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    report = build_morning_report(notion, datetime.now(KST).date())
    if report is None:
        print("[notify_hackathon_leads] 알릴 리드 없음")
        return

    profile = slack_client.users_info(user=OWNER_USER_ID)["user"]["profile"]
    away = is_vacation_profile(profile)
    recipient = BACKUP_USER_ID if away else OWNER_USER_ID
    text = report.text + (
        "\n\n_(이주현 님 휴가 중이라 김세윤 님께 보냅니다)_" if away else ""
    )

    if dry_run:
        print(f"[dry-run] DM → {recipient}\n{text}")
        for ts in report.due_thread_ts:
            print(f"[dry-run] 스레드 {ts} 에 오늘 연락 예정")
        return

    slack_client.chat_postMessage(channel=recipient, text=text, unfurl_links=False)
    for ts in report.due_thread_ts:
        slack_client.chat_postMessage(
            channel=CHANNEL_ID, thread_ts=ts, text=f"{SUMMARY_MARKER} 오늘 연락 예정"
        )
    print(
        f"[notify_hackathon_leads] DM → {recipient}, 스레드 {len(report.due_thread_ts)}건"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="해커톤 리드 아침 정리 DM")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 콘솔 출력")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
