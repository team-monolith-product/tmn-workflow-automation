import os
from datetime import datetime
from notion_client import Client as NotionClient
from slack_sdk import WebClient

DATABASE_ID: str = 'a9de18b3877c453a8e163c2ee1ff4137'
CHANNEL_ID: str = 'C02F56PACF7'

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

def alert_overdue_tasks():
    notion = NotionClient(auth=os.environ.get("NOTION_API_KEY"))
    today = datetime.now().date()

    # '진행' 상태이면서 타임라인 종료일이 today보다 과거인 페이지 검색
    results = notion.databases.query(
        **{
            "database_id": DATABASE_ID,
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

    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    slack_channel_id = CHANNEL_ID

    email_to_slack_id = get_slack_user_map(slack_client)
    for result in results.get("results", []):
        task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        page_url = result["url"]
        assignee_email = result["properties"]["담당자"]["people"][0]["person"]["email"]

        slack_user_id = email_to_slack_id.get(assignee_email)
        if slack_user_id:
            slack_client.chat_postMessage(
                channel=slack_channel_id,
                text=f"과업 <{page_url}|{task_name}>이(가) 기한이 지났습니다. <@{slack_user_id}> 확인 부탁드립니다."
            )
        else :
            slack_client.chat_postMessage(
                channel=slack_channel_id,
                text=f"과업 <{page_url}|{task_name}>이(가) 기한이 지났으나 담당자를 확인할 수 없습니다."
            )


if __name__ == "__main__":
    alert_overdue_tasks()
