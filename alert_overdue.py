import os
from datetime import datetime

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

# 환경 변수 로드
load_dotenv()

DATABASE_ID: str = 'a9de18b3877c453a8e163c2ee1ff4137'
CHANNEL_ID: str = 'C02F56PACF7'

def main():
    notion = NotionClient(auth=os.environ.get("NOTION_API_KEY"))
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    email_to_slack_id = get_slack_user_map(slack_client)

    alert_overdue_tasks(
        notion,
        slack_client,
        DATABASE_ID,
        CHANNEL_ID,
        email_to_slack_id
    )
    alert_no_due_tasks(
        notion,
        slack_client,
        DATABASE_ID,
        CHANNEL_ID,
        email_to_slack_id
    )

def get_slack_user_map(slack_client: WebClient):
    email_to_slack_id = {}
    cursor = None

    while True:
        response = slack_client.users_list(cursor=cursor)
        members = response["members"]

        for member in members:
            profile = member.get("profile", {})
            email = profile.get("email")
            if email:
                email_to_slack_id[email] = member["id"]

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return email_to_slack_id


def alert_overdue_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    database_id: str,
    channel_id: str,
    email_to_slack_id: dict,
):
    """
    진행 중인 과업 중 종료일이 지난 과업을 슬랙으로 알림

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        database_id (str): Notion database id
        channel_id (str): Slack channel id
        email_to_slack_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """
    today = datetime.now().date()

    # '진행' 상태이면서 타임라인 종료일이 today보다 과거인 페이지 검색
    results = notion.databases.query(
        **{
            "database_id": database_id,
            "filter": {
                "and": [
                    {
                        "property": "상태",
                        "status": {
                            "equals": "진행"
                        }
                    },
                    {
                        "property": "종료일",
                        "date": {
                            "before": today.isoformat()
                        }
                    }
                ]
            }
        }
    )

    for result in results.get("results", []):
        task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        page_url = result["url"]
        assignee_email = result["properties"]["담당자"]["people"][0]["person"]["email"]

        slack_user_id = email_to_slack_id.get(assignee_email)
        if slack_user_id:
            text = f"과업 <{page_url}|{task_name}>이(가) 기한이 지났습니다. <@{slack_user_id}> 확인 부탁드립니다."
        else:
            text = f"과업 <{page_url}|{task_name}>이(가) 기한이 지났으나 담당자를 확인할 수 없습니다."
        slack_client.chat_postMessage(channel=channel_id, text=text)


def alert_no_due_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    database_id: str,
    channel_id: str,
    email_to_slack_id: dict,
):
    """
    기간 산정 없이 진행 중인 과업을 슬랙으로 알림

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        database_id (str): Notion database id
        channel_id (str): Slack channel id
        email_to_slack_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """

    # '진행' 또는 '리뷰' 상태이면서 타임라인이 없는 페이지 검색
    results = notion.databases.query(
        **{
            "database_id": database_id,
            "filter": {
                "and": [
                    {
                        "or": [
                            {
                                "property": "상태",
                                "status": {
                                    "equals": "진행"
                                }
                            },
                            {
                                "property": "상태",
                                "status": {
                                    "equals": "리뷰"
                                }
                            }
                        ]
                    },
                    {
                        "property": "타임라인",
                        "date": {
                            "is_empty": True
                        }
                    }
                ]
            }
        }
    )

    for result in results.get("results", []):
        task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        page_url = result["url"]
        assignee_email = result["properties"]["담당자"]["people"][0]["person"]["email"]

        slack_user_id = email_to_slack_id.get(assignee_email)
        if slack_user_id:
            text = (
                f"과업 <{page_url}|{task_name}>이(가) 기한이 지정되지 않은채로 진행되고 있습니다."
                f"<@{slack_user_id}> 확인 부탁드립니다."
            )
        else:
            text = f"과업 <{page_url}|{task_name}>이(가) 기한이 지정되지 않은채로 진행되고 있으나 담당자를 확인할 수 없습니다."
        slack_client.chat_postMessage(channel=channel_id, text=text)


if __name__ == "__main__":
    main()
