import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

load_dotenv()

DATA_SOURCE_ID: str = "3281cc82-0da6-804b-8ff8-000b4dc45247"
DATABASE_ID: str = "3281cc82-0da6-801a-8355-ef6fa79cc1a9"
VIEW_ID: str = "3281cc820da68012b7e4000ca80d7977"
CHANNEL_ID: str = "C0B0V4ALE48"  # t_고객_고객관리
MENTION_USER_IDS: list[str] = ["U052HDXN3EG", "U090WMNEGQ1"]  # 이주현, 박기남


def main(dry_run: bool = False):
    """타입폼 DB에서 소통이 비어있는 항목을 매일 아침 알림"""
    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    entries = query_no_communication(notion)

    if not entries:
        return

    message = build_message(entries)

    if dry_run:
        print(f"[dry-run] {message}")
    else:
        slack_client.chat_postMessage(channel=CHANNEL_ID, text=message)


def query_no_communication(notion: NotionClient) -> list[dict]:
    """☎️ 소통 관계가 비어있는 항목을 모두 조회"""
    all_results = []
    start_cursor = None

    while True:
        kwargs = {
            "data_source_id": DATA_SOURCE_ID,
            "filter": {"property": "☎️ 소통", "relation": {"is_empty": True}},
        }
        if start_cursor:
            kwargs["start_cursor"] = start_cursor

        response = notion.data_sources.query(**kwargs)
        results = response.get("results", [])

        for result in results:
            try:
                name = result["properties"]["이름"]["title"][0]["text"]["content"]
            except (KeyError, IndexError):
                name = "이름 없음"

            institution_prop = result["properties"].get("기관", {})
            institution = (
                institution_prop.get("select", {}).get("name", "")
                if institution_prop.get("select")
                else ""
            )

            all_results.append(
                {"name": name, "institution": institution, "url": result["url"]}
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
        f"소통이 아직 진행되지 않은 항목이 *{len(entries)}건* 있습니다.",
        "",
    ]

    for entry in entries:
        institution = f" ({entry['institution']})" if entry["institution"] else ""
        lines.append(f"• <{entry['url']}|{entry['name']}>{institution}")

    lines.append("")
    lines.append(f"<{view_url}|노션에서 전체 목록 보기>")

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
