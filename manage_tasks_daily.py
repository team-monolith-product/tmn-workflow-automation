import os
from datetime import datetime

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

from service.slack import get_email_to_user_id

# 환경 변수 로드
load_dotenv()

MAIN_DATA_SOURCE_ID: str = "a9de18b3877c453a8e163c2ee1ff4137"
CONTENTS_DATA_SOURCE_ID: str = "a87afa9c63f6438381255db5d01e68d4"
MAIN_CHANNEL_ID: str = "C087PDC9VG8"
CONTENTS_CHANNEL_ID: str = "C091ZUBTCKU"


def main():
    notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03")
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    email_to_user_id = get_email_to_user_id(slack_client)

    # 메인 작업 DB 처리
    send_intro_message(slack_client, MAIN_CHANNEL_ID)
    alert_overdue_tasks(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
    )
    alert_no_due_tasks(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
    )
    alert_no_tasks(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
        "e",
    )
    alert_no_후속_작업(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
    )

    # 콘텐츠 DB 처리
    send_intro_message(slack_client, CONTENTS_CHANNEL_ID)
    alert_overdue_tasks(
        notion,
        slack_client,
        CONTENTS_DATA_SOURCE_ID,
        CONTENTS_CHANNEL_ID,
        email_to_user_id,
    )
    alert_no_due_tasks(
        notion,
        slack_client,
        CONTENTS_DATA_SOURCE_ID,
        CONTENTS_CHANNEL_ID,
        email_to_user_id,
    )
    alert_no_tasks(
        notion,
        slack_client,
        CONTENTS_DATA_SOURCE_ID,
        CONTENTS_CHANNEL_ID,
        email_to_user_id,
        "콘텐츠",
    )


def send_intro_message(
    slack_client: WebClient,
    channel_id: str,
):
    """
    지연된 작업에 대한 인트로 메시지를 전송하는 함수

    Args:
        slack_client (WebClient): Slack
        channel_id (str): Slack channel id

    Returns:
        None
    """
    intro_message = (
        "좋은 아침입니다! \n"
        "아래 지연된 작업에 대해 적절한 사유를 댓글로 남기고, 로봇을 통해 일정을 변경해주시길 부탁드립니다.\n"
        "항상 협조해 주셔서 감사합니다."
    )
    slack_client.chat_postMessage(channel=channel_id, text=intro_message)


def alert_overdue_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
):
    """
    대기 및 진행 중인 작업 중 종료일이 지난 작업을 슬랙으로 알림

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        data_source_id (str): Notion data_source id
        channel_id (str): Slack channel id
        email_to_user_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """
    today = datetime.now().date()

    # 대기 또는 진행 상태이면서 타임라인 종료일이 today보다 과거인 페이지 검색
    results = notion.data_sources.query(
        **{
            "data_source_id": data_source_id,
            "filter": {
                "and": [
                    {
                        "or": [
                            {"property": "상태", "status": {"equals": "대기"}},
                            {"property": "상태", "status": {"equals": "진행"}},
                            {"property": "상태", "status": {"equals": "리뷰"}},
                        ]
                    },
                    {"property": "종료일", "date": {"before": today.isoformat()}},
                ]
            },
        }
    )

    for result in results.get("results", []):
        try:
            task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        except (KeyError, IndexError):
            task_name = "제목 없음"
        page_url = result["url"]
        people = result["properties"]["담당자"]["people"]
        if people:
            person = people[0].get("person")
            if person:
                assignee_email = person["email"]
                slack_user_id = email_to_user_id.get(assignee_email)
            else:
                slack_user_id = None
        else:
            slack_user_id = None

        if slack_user_id:
            text = f"작업 <{page_url}|{task_name}>이(가) 기한이 지났습니다. <@{slack_user_id}> 확인 부탁드립니다."
        else:
            text = f"작업 <{page_url}|{task_name}>이(가) 기한이 지났으나 담당자를 확인할 수 없습니다."
        slack_client.chat_postMessage(channel=channel_id, text=text)


def alert_no_due_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
):
    """
    기간 산정 없이 진행 중인 작업을 슬랙으로 알림

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        data_source_id (str): Notion data_source id
        channel_id (str): Slack channel id
        email_to_user_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """

    # '진행' 또는 '리뷰' 상태이면서 타임라인이 없는 페이지 검색
    results = notion.data_sources.query(
        **{
            "data_source_id": data_source_id,
            "filter": {
                "and": [
                    {
                        "or": [
                            {"property": "상태", "status": {"equals": "진행"}},
                            {"property": "상태", "status": {"equals": "리뷰"}},
                        ]
                    },
                    {"property": "타임라인", "date": {"is_empty": True}},
                ]
            },
        }
    )

    for result in results.get("results", []):
        try:
            task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        except (KeyError, IndexError):
            task_name = "제목 없음"
        page_url = result["url"]
        people = result["properties"]["담당자"]["people"]
        if people:
            person = people[0].get("person")
            if person:
                assignee_email = person["email"]
                slack_user_id = email_to_user_id.get(assignee_email)
            else:
                slack_user_id = None
        else:
            slack_user_id = None

        if slack_user_id:
            text = (
                f"작업 <{page_url}|{task_name}>이(가) 기한이 지정되지 않은채로 진행되고 있습니다."
                f"<@{slack_user_id}> 확인 부탁드립니다."
            )
        else:
            text = f"작업 <{page_url}|{task_name}>이(가) 기한이 지정되지 않은채로 진행되고 있으나 담당자를 확인할 수 없습니다."
        slack_client.chat_postMessage(channel=channel_id, text=text)


def alert_no_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
    group_handle: str,
):
    """
    아무 작업도 진행 중이지 않은 작업자를 슬랙으로 알림

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        data_source_id (str): Notion data_source id
        channel_id (str): Slack channel id
        email_to_user_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리
        group_handle (str): Slack 사용자 그룹 핸들 (예: "e", "콘텐츠")

    Returns:
        None
    """
    # 1. 현재 '진행' 혹은 '리뷰' 상태인 작업의 담당자 이메일들을 모두 가져옵니다.
    in_progress_tasks = notion.data_sources.query(
        **{
            "data_source_id": data_source_id,
            "filter": {
                "or": [
                    {"property": "상태", "status": {"equals": "진행"}},
                    {"property": "상태", "status": {"equals": "리뷰"}},
                ]
            },
        }
    )

    assigned_emails = set()
    for task in in_progress_tasks.get("results", []):
        people = task["properties"]["담당자"].get("people", [])
        for person in people:
            person_info = person.get("person")
            if person_info:
                email = person_info.get("email")
                if email:
                    assigned_emails.add(email)

    # 2. Slack 사용자 그룹 목록 중에서 지정된 handle인 그룹을 찾습니다.
    usergroup_id = None
    usergroups_response = slack_client.usergroups_list()
    for group in usergroups_response["usergroups"]:
        # 지정된 handle인 사용자 그룹 찾아 ID 획득
        if group["handle"] == group_handle:
            usergroup_id = group["id"]
            break

    # 3. 찾은 사용자 그룹의 멤버들을 조회하여, 각 Slack user ID를 얻습니다.
    if usergroup_id is None:
        slack_client.chat_postMessage(
            channel=channel_id,
            text=f"그룹 @{group_handle}를 찾을 수 없습니다. 확인부탁드립니다.",
        )
        return

    group_users_response = slack_client.usergroups_users_list(usergroup=usergroup_id)
    group_user_ids = group_users_response.get("users", [])

    # 4. email_to_user_id는 "email -> slack user id" 매핑이므로,
    #    그 반대("slack user id -> email") 매핑을 쉽게 얻기 위해 역으로 변환합니다.
    user_id_to_email = {v: k for k, v in email_to_user_id.items()}

    # 5. 그룹에 실제 등록된 멤버의 이메일 목록
    team_emails = []
    for user_id in group_user_ids:
        email = user_id_to_email.get(user_id)
        if email:
            team_emails.append(email)

    # 6. "아무 작업도 진행 중이지 않은" ⇒ 팀 멤버 중 assigned_emails에 없는 이메일
    unassigned_emails = set(team_emails) - assigned_emails

    # 7. unassigned_emails에 속한 멤버들에게 알림 보내기
    for email in unassigned_emails:
        slack_user_id = email_to_user_id.get(email)
        if slack_user_id:
            text = (
                f"<@{slack_user_id}> 현재 진행중인 작업이 없습니다. "
                "혹시 진행해야 할 업무가 누락되지 않았는지 확인 부탁드립니다."
            )
        else:
            # 혹시라도 email_to_user_id에 매핑되어 있지 않은 경우 처리
            text = (
                f"{email}님께서 현재 진행중인 작업이 없습니다. "
                "혹시 진행해야 할 업무가 누락되지 않았는지 확인 부탁드립니다. "
                "또한 이메일 매핑이 누락된 원인을 파악해주시길 바랍니다."
            )
        slack_client.chat_postMessage(channel=channel_id, text=text)


def alert_no_후속_작업(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
):
    """
    후속 작업이 마땅히 예상 되나 후속 작업이 등록되지 않은 경우 알림 (메인 작업 DB 전용)
    - '구성요소' 다중 선택 속성에 기획 또는 디자인이 들어있는 경우
    - '상태' 속성이 '완료'인 경우
    - '후속 작업'(관계형) 속성이 비어 있는 경우
    - '작성일시'(생성 일시)가 2025년 1월 1일 이후인 경우
    - 단, 제목에 '후속 작업 없음'이 포함된 경우는 제외

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        data_source_id (str): Notion data_source id
        channel_id (str): Slack channel id
        email_to_user_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리

    Returns:
        None
    """
    # 메인 작업 DB용 쿼리
    query_filter = {
        "and": [
            {
                "property": "작성일시",
                "created_time": {"on_or_after": "2025-01-01T00:00:00.000Z"},
            },
            {"property": "상태", "status": {"equals": "완료"}},
            {
                "or": [
                    {"property": "구성요소", "multi_select": {"contains": "기획"}},
                    {
                        "property": "구성요소",
                        "multi_select": {"contains": "디자인"},
                    },
                ]
            },
            {"property": "후속 작업", "relation": {"is_empty": True}},
            {"property": "제목", "title": {"does_not_contain": "후속 작업 없음"}},
        ]
    }

    results = notion.data_sources.query(
        **{"data_source_id": data_source_id, "filter": query_filter}
    )

    for result in results.get("results", []):
        try:
            task_name = result["properties"]["제목"]["title"][0]["text"]["content"]
        except (KeyError, IndexError):
            task_name = "제목 없음"
        page_url = result["url"]

        people = result["properties"]["담당자"]["people"]
        if people:
            person = people[0].get("person")
            if person:
                assignee_email = person["email"]
                slack_user_id = email_to_user_id.get(assignee_email)
            else:
                slack_user_id = None
        else:
            slack_user_id = None

        if slack_user_id:
            text = (
                f"작업 <{page_url}|{task_name}>은(는) 작업이 완료되었습니다만, "
                "아직 후속 작업이 등록되어 있지 않습니다.\n"
                f"<@{slack_user_id}> 확인 부탁드립니다."
            )
        else:
            text = (
                f"작업 <{page_url}|{task_name}>은(는) 작업이 완료되었으나, "
                "담당자를 확인할 수 없고 후속 작업도 등록되어 있지 않습니다.\n"
                "Notion에서 담당자/후속 작업 정보를 업데이트 부탁드립니다."
            )
        slack_client.chat_postMessage(channel=channel_id, text=text)


if __name__ == "__main__":
    main()
