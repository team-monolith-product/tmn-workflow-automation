import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

load_dotenv()

DATA_SOURCE_ID: str = "32e1cc82-0da6-8002-a702-000b5d28074f"
DATABASE_ID: str = "32e1cc820da68002b9b9d3b1f3229b93"
VIEW_ID: str = "3591cc820da6800a9deb000c82d125bd"
CHANNEL_ID: str = "C0B0V4ALE48"  # t_고객_고객관리
MENTION_USER_IDS: list[str] = ["U052HDXN3EG", "U090WMNEGQ1"]  # 이주현, 박기남


def main(dry_run: bool = False):
    """코들 전화번호 인증 후 리드 미추가 건 매일 아침 알림"""
    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    entries = query_lead_missing(notion)

    if not entries:
        return

    message = build_message(entries)

    if dry_run:
        print(f"[dry-run] {message}")
    else:
        slack_client.chat_postMessage(channel=CHANNEL_ID, text=message)


def query_lead_missing(notion: NotionClient) -> list[dict]:
    """전화번호 인증은 했으나 리드가 비어있는 항목을 모두 조회"""
    all_results = []
    start_cursor = None

    while True:
        kwargs = {
            "data_source_id": DATA_SOURCE_ID,
            "filter": {
                "and": [
                    {
                        "property": "phone_number",
                        "phone_number": {"is_not_empty": True},
                    },
                    {"property": "\ud83d\udc69\u200d\ud83c\udfeb \ub9ac\ub4dc", "relation": {"is_empty": True}},
                ]
            },
        }
        if start_cursor:
            kwargs["start_cursor"] = start_cursor

        response = notion.data_sources.query(**kwargs)
        results = response.get("results", [])

        for result in results:
            try:
                user_name = result["properties"]["user_name"]["rich_text"][0][
                    "plain_text"
                ]
            except (KeyError, IndexError):
                user_name = "이름 없음"

            try:
                school = result["properties"]["학교"]["rich_text"][0]["plain_text"]
            except (KeyError, IndexError):
                school = ""

            all_results.append(
                {"user_name": user_name, "school": school, "url": result["url"]}
            )

        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")

    return all_results


def build_message(entries: list[dict]) -> str:
    """슬랙 알림 메시지 생성"""
    view_url = f"https://www.notion.so/team-mono/{DATABASE_ID}?v={VIEW_ID}"
    mentions = " ".join(f"<@{uid}>" for uid in MENTION_USER_IDS)

    lines = [
        f"{mentions}",
        f"코들에서 전화번호 인증을 했으나 리드가 추가되지 않은 항목이 *{len(entries)}건* 있습니다.",
        "",
    ]

    for entry in entries:
        school = f" ({entry['school']})" if entry["school"] else ""
        lines.append(f"\u2022 <{entry['url']}|{entry['user_name']}>{school}")

    lines.append("")
    lines.append(f"<{view_url}|\ub178\uc158\uc5d0\uc11c \uc804\uccb4 \ubaa9\ub85d \ubcf4\uae30>")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack 메시지를 보내지 않고 콘솔에 출력만",
    )
    args = parser.parse_args()

    main(dry_run=args.dry_run)
