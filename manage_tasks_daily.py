import os
from datetime import datetime

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

# 환경 변수 로드
load_dotenv()

DATABASE_ID: str = 'a9de18b3877c453a8e163c2ee1ff4137'
CHANNEL_ID: str = 'C087PDC9VG8'


def main():
    notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    email_to_slack_id = get_slack_user_map(slack_client)

    send_intro_message(slack_client, CHANNEL_ID)
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
    alert_no_tasks(
        notion,
        slack_client,
        DATABASE_ID,
        CHANNEL_ID,
        email_to_slack_id
    )
    alert_no_후속_작업(
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


def send_intro_message(
    slack_client: WebClient,
    channel_id: str,
):
    """
    지연된 과업에 대한 인트로 메시지를 전송하는 함수

    Args:
        slack_client (WebClient): Slack
        channel_id (str): Slack channel id

    Returns:
        None
    """
    intro_message = (
        "좋은 아침입니다! \n"
        "아래 지연된 과업에 대해 적절한 사유를 댓글로 남기고, 로봇을 통해 일정을 변경해주시길 부탁드립니다.\n"
        "항상 협조해 주셔서 감사합니다."
    )
    slack_client.chat_postMessage(channel=channel_id, text=intro_message)

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
        people = result["properties"]["담당자"]["people"]
        if people:
            assignee_email = people[0]["person"]["email"]
            slack_user_id = email_to_slack_id.get(assignee_email)
        else:
            slack_user_id = None

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
        people = result["properties"]["담당자"]["people"]
        if people:
            assignee_email = people[0]["person"]["email"]
            slack_user_id = email_to_slack_id.get(assignee_email)
        else:
            slack_user_id = None

        if slack_user_id:
            text = (
                f"과업 <{page_url}|{task_name}>이(가) 기한이 지정되지 않은채로 진행되고 있습니다."
                f"<@{slack_user_id}> 확인 부탁드립니다."
            )
        else:
            text = f"과업 <{page_url}|{task_name}>이(가) 기한이 지정되지 않은채로 진행되고 있으나 담당자를 확인할 수 없습니다."
        slack_client.chat_postMessage(channel=channel_id, text=text)


def alert_no_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    database_id: str,
    channel_id: str,
    email_to_slack_id: dict,
):
    """
    아무 과업도 진행 중이지 않은 작업자를 슬랙으로 알림 (단, @e 그룹에 속한 멤버만 대상)

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        database_id (str): Notion database id
        channel_id (str): Slack channel id
        email_to_slack_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """
    # 1. 현재 '진행' 혹은 '리뷰' 상태인 과업의 담당자 이메일들을 모두 가져옵니다.
    in_progress_tasks = notion.databases.query(
        **{
            "database_id": database_id,
            "filter": {
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
            }
        }
    )

    assigned_emails = set()
    for task in in_progress_tasks.get("results", []):
        people = task["properties"]["담당자"].get("people", [])
        for person in people:
            email = person["person"].get("email")
            if email:
                assigned_emails.add(email)

    # 2. Slack 사용자 그룹 목록 중에서 handle이 "e"인 그룹을 찾습니다. (예: @e 그룹)
    usergroup_id = None
    usergroups_response = slack_client.usergroups_list()
    for group in usergroups_response["usergroups"]:
        # handle이 'e'인 사용자 그룹 찾아 ID 획득
        if group["handle"] == "e":
            usergroup_id = group["id"]
            break

    # 3. 찾은 사용자 그룹의 멤버들을 조회하여, 각 Slack user ID를 얻습니다.
    if usergroup_id is None:
        slack_client.chat_postMessage(
            channel=channel_id,
            text="엔지니어 그룹 @e를 찾을 수 없습니다. 확인부탁드립니다."
        )
        return

    e_group_users_response = slack_client.usergroups_users_list(
        usergroup=usergroup_id)
    e_user_ids = e_group_users_response.get("users", [])

    # 4. email_to_slack_id는 "email -> slack user id" 매핑이므로,
    #    그 반대("slack user id -> email") 매핑을 쉽게 얻기 위해 역으로 변환합니다.
    slack_id_to_email = {v: k for k, v in email_to_slack_id.items()}

    # 5. @e 그룹에 실제 등록된 멤버의 이메일 목록
    team_e_emails = []
    for user_id in e_user_ids:
        email = slack_id_to_email.get(user_id)
        if email:
            team_e_emails.append(email)

    # 6. "아무 과업도 진행 중이지 않은" ⇒ 팀 E 멤버 중 assigned_emails에 없는 이메일
    unassigned_emails = set(team_e_emails) - assigned_emails

    # 7. unassigned_emails에 속한 멤버들에게 알림 보내기
    for email in unassigned_emails:
        slack_user_id = email_to_slack_id.get(email)
        if slack_user_id:
            text = (
                f"<@{slack_user_id}> 현재 진행중인 과업이 없습니다. "
                "혹시 진행해야 할 업무가 누락되지 않았는지 확인 부탁드립니다."
            )
        else:
            # 혹시라도 email_to_slack_id에 매핑되어 있지 않은 경우 처리
            text = (
                f"{email}님께서 현재 진행중인 과업이 없습니다. "
                "혹시 진행해야 할 업무가 누락되지 않았는지 확인 부탁드립니다."
                "또한 이메일 매핑이 누락된 원인을 파악해주시길 바랍니다."
            )
        slack_client.chat_postMessage(channel=channel_id, text=text)

def alert_no_후속_작업(
    notion: NotionClient,
    slack_client: WebClient,
    database_id: str,
    channel_id: str,
    email_to_slack_id: dict,
):
    """
    후속 작업이 마땅히 예상 되나 후속 작업이 등록되지 않은 경우 알림.
    - '구성요소' 다중 선택 속성에 Plan 또는 Design이 들어있는 경우
    - '상태' 속성이 '완료'인 경우
    - '후속 작업'(관계형) 속성이 비어 있는 경우
    - '작성일시'(생성 일시)가 2025년 1월 1일 이후인 경우
    
    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        database_id (str): Notion database id
        channel_id (str): Slack channel id
        email_to_slack_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """
    query_filter = {
        "and": [
            {
                "property": "작성일시",
                "created_time": {
                    "on_or_after": "2025-01-01T00:00:00.000Z"
                }
            },
            {
                "property": "상태",
                "status": {
                    "equals": "완료"
                }
            },
            {
                "or": [
                    {
                        "property": "구성요소",
                        "multi_select": {
                            "contains": "Plan"
                        }
                    },
                    {
                        "property": "구성요소",
                        "multi_select": {
                            "contains": "Design"
                        }
                    }
                ]
            },
            {
                "property": "후속 작업",
                "relation": {
                    "is_empty": True
                }
            }
        ]
    }

    results = notion.databases.query(
        **{
            "database_id": database_id,
            "filter": query_filter
        }
    )

    for result in results.get("results", []):
        task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        page_url = result["url"]

        people = result["properties"]["담당자"]["people"]
        if people:
            assignee_email = people[0]["person"]["email"]
            slack_user_id = email_to_slack_id.get(assignee_email)
        else:
            slack_user_id = None

        if slack_user_id:
            text = (
                f"과업 <{page_url}|{task_name}>은(는) 작업이 완료되었습니다만, "
                "아직 **후속 작업**이 등록되어 있지 않습니다.\n"
                f"<@{slack_user_id}> 확인 부탁드립니다."
            )
        else:
            text = (
                f"과업 <{page_url}|{task_name}>은(는) 작업이 완료되었으나, "
                "담당자를 확인할 수 없고 **후속 작업**도 등록되어 있지 않습니다.\n"
                "Notion에서 담당자/후속 작업 정보를 업데이트 부탁드립니다."
            )
        slack_client.chat_postMessage(channel=channel_id, text=text)

if __name__ == "__main__":
    main()
