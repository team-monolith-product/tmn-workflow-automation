import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

from service.slack import get_email_to_user_id

# 환경 변수 로드
load_dotenv()

DATA_SOURCE_ID: str = "2ae51eb0-a108-435f-95aa-802aaab6812f"
CHANNEL_ID: str = "C0AH3KVLPLH"
CREATED_AFTER: str = "2026-02-24T00:00:00.000Z"


def main(dry_run: bool = False):
    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN_JUSTIN"))

    email_to_user_id = get_email_to_user_id(slack_client)

    alert_missing_author(notion, slack_client, email_to_user_id, dry_run)


def alert_missing_author(
    notion: NotionClient,
    slack_client: WebClient,
    email_to_user_id: dict[str, str],
    dry_run: bool = False,
):
    """
    작성자가 비어있는 보고서를 찾아 생성자에게 작성자 입력을 요청

    Args:
        notion (NotionClient): Notion 클라이언트
        slack_client (WebClient): Slack 클라이언트
        email_to_user_id (dict[str, str]): 이메일 → Slack ID 매핑
        dry_run (bool): True이면 Slack 전송 없이 콘솔 출력만
    """
    results = notion.data_sources.query(
        **{
            "data_source_id": DATA_SOURCE_ID,
            "filter": {
                "and": [
                    {
                        "property": "작성 일시",
                        "created_time": {"on_or_after": CREATED_AFTER},
                    },
                    {"property": "작성자", "people": {"is_empty": True}},
                ]
            },
        }
    )

    for result in results.get("results", []):
        try:
            title = result["properties"]["Name"]["title"][0]["text"]["content"]
        except (KeyError, IndexError):
            title = "제목 없음"

        page_url = result["url"]

        created_by = result["properties"]["생성자"]["created_by"]
        creator_email = created_by["person"]["email"]
        creator_name = created_by.get("name", creator_email)
        slack_user_id = email_to_user_id.get(creator_email)

        if slack_user_id:
            mention = f"<@{slack_user_id}>"
        else:
            mention = creator_name

        text = (
            f"<{page_url}|{title}> 보고서에 작성자가 입력되지 않았습니다. "
            f"{mention} 작성자를 입력해주세요."
        )

        if dry_run:
            print(f"[dry-run] {text}")
        else:
            slack_client.chat_postMessage(channel=CHANNEL_ID, text=text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack 메시지를 보내지 않고 콘솔에 출력만",
    )
    args = parser.parse_args()

    main(dry_run=args.dry_run)
