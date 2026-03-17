import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from openai import OpenAI
from slack_sdk import WebClient

from service.business_days import get_nth_business_day_from
from service.holidays import get_public_holidays
from service.slack import get_email_to_user_id

# 환경 변수 로드
load_dotenv()

MAIN_DATA_SOURCE_ID: str = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"
CONTENTS_DATA_SOURCE_ID: str = "fecd7fca-8280-4f02-b78f-7fa720f53aa6"
MAIN_CHANNEL_ID: str = "C087PDC9VG8"
CONTENTS_CHANNEL_ID: str = "C091ZUBTCKU"


def main():
    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
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
    alert_pending_but_started_tasks(
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
    alert_no_upcoming_tasks(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
        "e",
        exclude_group_handles=["ie"],
    )
    # https://www.notion.so/team-mono/25Y-11M-2a11cc820da68051bab8ea146ee3001e?source=copy_link#2a41cc820da680daa823ff847717f6bf
    # alert_no_후속_작업(
    #     notion,
    #     slack_client,
    #     MAIN_DATA_SOURCE_ID,
    #     MAIN_CHANNEL_ID,
    #     email_to_user_id,
    # )
    alert_schedule_feasibility(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
        "e",
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
    alert_pending_but_started_tasks(
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
                    {
                        "property": "종료일",
                        "formula": {"date": {"before": today.isoformat()}},
                    },
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


def alert_pending_but_started_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
):
    """
    시작일이 지났으나 아직 대기 상태인 작업을 슬랙으로 알림

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

    # 대기 상태이면서 시작일이 today보다 과거인 페이지 검색
    results = notion.data_sources.query(
        **{
            "data_source_id": data_source_id,
            "filter": {
                "and": [
                    {"property": "상태", "status": {"equals": "대기"}},
                    {
                        "property": "시작일",
                        "formula": {"date": {"before": today.isoformat()}},
                    },
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
            text = f"작업 <{page_url}|{task_name}>이(가) 시작일이 지났으나 아직 대기 상태입니다. <@{slack_user_id}> 확인 부탁드립니다."
        else:
            text = f"작업 <{page_url}|{task_name}>이(가) 시작일이 지났으나 아직 대기 상태이며, 담당자를 확인할 수 없습니다."
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


def alert_no_upcoming_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
    group_handle: str,
    exclude_group_handles: list[str] | None = None,
):
    """
    5일 후에 예정된 작업이 없는 작업자를 예진님에게 알림

    Args:
        notion (NotionClient): Notion
        slack_client (WebClient): Slack
        data_source_id (str): Notion data_source id
        channel_id (str): Slack channel id
        email_to_user_id (dict): 이메일 주소를 슬랙 id로 매핑한 딕셔너리
        group_handle (str): Slack 사용자 그룹 핸들 (예: "e", "콘텐츠")
        exclude_group_handles (list[str] | None): 제외할 Slack 사용자 그룹 핸들 목록 (예: ["ie"])

    Returns:
        None
    """
    # 예진님 Slack ID
    YEJIN_SLACK_ID = "U075PUFNGHX"

    # 영업일 기준 5일 후 날짜 계산
    target_date = get_nth_business_day_from(datetime.now().date(), 5)

    # 5일 후에 진행 중일 것으로 예상되는 작업들을 찾습니다.
    # 시작일 <= 5일 후 AND 종료일 >= 5일 후 AND 상태가 완료/보류가 아닌 작업
    upcoming_tasks = notion.data_sources.query(
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
                    {
                        "property": "시작일",
                        "formula": {"date": {"on_or_before": target_date.isoformat()}},
                    },
                    {
                        "property": "종료일",
                        "formula": {"date": {"on_or_after": target_date.isoformat()}},
                    },
                ]
            },
        }
    )

    # 5일 후에 작업이 예정된 담당자 이메일 수집
    assigned_emails = set()
    for task in upcoming_tasks.get("results", []):
        people = task["properties"]["담당자"].get("people", [])
        for person in people:
            person_info = person.get("person")
            if person_info:
                email = person_info.get("email")
                if email:
                    assigned_emails.add(email)

    # Slack 사용자 그룹에서 지정된 handle인 그룹을 찾습니다.
    usergroup_id = None
    usergroups_response = slack_client.usergroups_list()
    for group in usergroups_response["usergroups"]:
        if group["handle"] == group_handle:
            usergroup_id = group["id"]
            break

    if usergroup_id is None:
        slack_client.chat_postMessage(
            channel=channel_id,
            text=f"그룹 @{group_handle}를 찾을 수 없습니다. 확인부탁드립니다.",
        )
        return

    group_users_response = slack_client.usergroups_users_list(usergroup=usergroup_id)
    group_user_ids = set(group_users_response.get("users", []))

    # 제외할 그룹 멤버 필터링
    if exclude_group_handles:
        for exclude_handle in exclude_group_handles:
            exclude_usergroup_id = None
            for group in usergroups_response["usergroups"]:
                if group["handle"] == exclude_handle:
                    exclude_usergroup_id = group["id"]
                    break
            if exclude_usergroup_id:
                exclude_response = slack_client.usergroups_users_list(
                    usergroup=exclude_usergroup_id
                )
                exclude_user_ids = set(exclude_response.get("users", []))
                group_user_ids -= exclude_user_ids

    # "slack user id -> email" 매핑 생성
    user_id_to_email = {v: k for k, v in email_to_user_id.items()}

    # 그룹에 등록된 멤버의 이메일 목록
    team_emails = []
    for user_id in group_user_ids:
        email = user_id_to_email.get(user_id)
        if email:
            team_emails.append(email)

    # 5일 후에 예정된 작업이 없는 멤버 찾기
    unassigned_emails = set(team_emails) - assigned_emails

    # 예진님에게 알림 보내기
    if unassigned_emails:
        member_mentions = []
        for email in unassigned_emails:
            slack_user_id = email_to_user_id.get(email)
            if slack_user_id:
                member_mentions.append(f"<@{slack_user_id}>")
            else:
                member_mentions.append(email)

        members_text = ", ".join(member_mentions)
        text = (
            f"<@{YEJIN_SLACK_ID}> 5일 후에 예정된 작업이 없는 멤버가 있습니다: {members_text}\n"
            "로드맵 점검 부탁드립니다."
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


def alert_schedule_feasibility(
    notion: NotionClient,
    slack_client: WebClient,
    data_source_id: str,
    channel_id: str,
    email_to_user_id: dict,
    group_handle: str,
    dry_run: bool = False,
):
    """
    각 담당자의 일정 실현 가능성을 LLM으로 평가하여 문제가 있는 경우 알림

    Args:
        notion: Notion 클라이언트
        slack_client: Slack 클라이언트
        data_source_id: 노션 데이터 소스 ID
        channel_id: Slack 채널 ID
        email_to_user_id: 이메일-Slack ID 매핑
        group_handle: Slack 사용자 그룹 핸들
        dry_run: True이면 Slack 전송 없이 콘솔 출력만
    """
    today = datetime.now().date()

    # 1. 대상 그룹의 멤버 이메일 목록 가져오기
    usergroup_id = None
    usergroups_response = slack_client.usergroups_list()
    for group in usergroups_response["usergroups"]:
        if group["handle"] == group_handle:
            usergroup_id = group["id"]
            break

    if usergroup_id is None:
        print(f"[dry-run] 그룹 '{group_handle}'을 찾을 수 없습니다.")
        return

    group_users_response = slack_client.usergroups_users_list(usergroup=usergroup_id)
    group_user_ids = group_users_response.get("users", [])

    user_id_to_email = {v: k for k, v in email_to_user_id.items()}
    target_emails = set()
    for user_id in group_user_ids:
        email = user_id_to_email.get(user_id)
        if email:
            target_emails.add(email)

    if dry_run:
        print(f"[dry-run] 대상 그룹: {group_handle}, 멤버 수: {len(target_emails)}")

    # 2. 진행 중이거나 예정된 작업 조회 (대기/진행/리뷰 상태 + 타임라인 있음) - pagination 처리
    all_results = []
    query_filter = {
        "and": [
            {
                "or": [
                    {"property": "상태", "status": {"equals": "대기"}},
                    {"property": "상태", "status": {"equals": "진행"}},
                    {"property": "상태", "status": {"equals": "리뷰"}},
                ]
            },
            {"property": "타임라인", "date": {"is_not_empty": True}},
        ]
    }
    has_more = True
    next_cursor = None

    while has_more:
        kwargs = {"data_source_id": data_source_id, "filter": query_filter}
        if next_cursor:
            kwargs["start_cursor"] = next_cursor

        results = notion.data_sources.query(**kwargs)
        all_results.extend(results.get("results", []))
        has_more = results.get("has_more", False)
        next_cursor = results.get("next_cursor")

    # 3. 담당자별로 작업 그룹화
    assignee_tasks: dict[str, list[dict]] = defaultdict(list)

    for result in all_results:
        task_info = _extract_task_info(result)
        assignee_email = task_info.get("assignee_email")

        if not assignee_email or assignee_email not in target_emails:
            continue

        assignee_tasks[assignee_email].append(task_info)

    if dry_run:
        total_tasks = sum(len(t) for t in assignee_tasks.values())
        print(
            f"[dry-run] 조회된 작업: {len(all_results)}개, 평가 대상: {total_tasks}개"
        )

    # 4. 각 담당자별로 LLM 평가 (병렬 처리)
    eval_targets = []
    for assignee_email, tasks in assignee_tasks.items():
        if len(tasks) < 2:  # 작업이 2개 미만이면 평가 스킵
            if dry_run:
                assignee_name = (
                    tasks[0].get("assignee_name") if tasks else assignee_email
                )
                print(
                    f"[dry-run] {assignee_name or assignee_email}: 작업 {len(tasks)}개 - 스킵"
                )
            continue

        assignee_name = tasks[0].get("assignee_name") or assignee_email
        tasks_text = _format_tasks_for_llm(tasks, today)
        eval_targets.append((assignee_email, assignee_name, tasks, tasks_text))

    def evaluate_single(target):
        assignee_email, assignee_name, tasks, tasks_text = target
        evaluation = _evaluate_schedule_with_llm(assignee_name, tasks_text)
        return assignee_email, assignee_name, tasks, tasks_text, evaluation

    # 병렬로 LLM 평가 실행
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(evaluate_single, t): t for t in eval_targets}

        for future in as_completed(futures):
            assignee_email, assignee_name, tasks, tasks_text, evaluation = (
                future.result()
            )

            if dry_run:
                print(f"\n[dry-run] === {assignee_name} ({len(tasks)}개 작업) ===")
                print(tasks_text)
                status = (
                    "가능"
                    if evaluation["is_feasible"] is True
                    else (
                        "불가능" if evaluation["is_feasible"] is False else "주의필요"
                    )
                )
                print(f"[dry-run] 평가 결과: {status}")
                print(evaluation["full_response"])
                print()

            # 실현 불가능하거나 주의 필요한 경우에만 알림
            if evaluation["is_feasible"] is not True:
                _send_schedule_alert(
                    slack_client,
                    channel_id,
                    assignee_email,
                    assignee_name,
                    evaluation,
                    tasks,
                    email_to_user_id,
                    dry_run,
                )


def _extract_task_info(result: dict) -> dict:
    """노션 쿼리 결과에서 작업 정보를 추출"""
    try:
        title = result["properties"]["제목"]["title"][0]["text"]["content"]
    except (KeyError, IndexError):
        title = "제목 없음"

    status = (
        result["properties"].get("상태", {}).get("status", {}).get("name", "알 수 없음")
    )

    timeline = result["properties"].get("타임라인", {}).get("date", {})
    start_date = timeline.get("start") if timeline else None
    end_date = timeline.get("end") if timeline else None

    # 시작일/종료일이 formula 타입일 수 있음
    if not start_date:
        start_prop = result["properties"].get("시작일", {})
        if start_prop.get("type") == "formula":
            formula = start_prop.get("formula", {})
            start_date = formula.get("string") or formula.get("date", {}).get("start")
        elif start_prop.get("type") == "date":
            start_date = start_prop.get("date", {}).get("start")

    if not end_date:
        end_prop = result["properties"].get("종료일", {})
        if end_prop.get("type") == "formula":
            formula = end_prop.get("formula", {})
            end_date = formula.get("string") or formula.get("date", {}).get("start")
        elif end_prop.get("type") == "date":
            end_date = end_prop.get("date", {}).get("start")

    people = result["properties"].get("담당자", {}).get("people", [])
    assignee_email = None
    assignee_name = None
    if people:
        person = people[0].get("person")
        if person:
            assignee_email = person.get("email")
            assignee_name = people[0].get("name")

    components = result["properties"].get("구성요소", {}).get("multi_select", [])
    component_names = [c["name"] for c in components]

    return {
        "title": title,
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "assignee_email": assignee_email,
        "assignee_name": assignee_name,
        "components": component_names,
        "url": result.get("url", ""),
    }


def _format_tasks_for_llm(tasks: list[dict], today) -> str:
    """LLM에게 전달할 작업 목록 텍스트 생성 (영업일 정보 포함)"""
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    today_weekday = weekday_names[today.weekday()]

    # 향후 2개월 공휴일 조회
    holidays = get_public_holidays(today.year, today.month)
    next_month = today.month + 1
    next_year = today.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    holidays.update(get_public_holidays(next_year, next_month))

    # 향후 4주간의 영업일 수 계산
    business_days_info = []
    for week_offset in range(4):
        week_start = today + timedelta(days=7 * week_offset)
        week_end = week_start + timedelta(days=6)
        biz_days = 0
        for d in range(7):
            check_date = week_start + timedelta(days=d)
            if check_date.weekday() < 5 and check_date.isoformat() not in holidays:
                biz_days += 1
        business_days_info.append(
            f"  - {week_start.strftime('%m/%d')}~{week_end.strftime('%m/%d')}: 영업일 {biz_days}일"
        )

    lines = [
        f"오늘 날짜: {today.isoformat()} ({today_weekday}요일)",
        "",
        "향후 4주간 영업일 현황:",
        *business_days_info,
        "",
        "작업 목록:",
    ]

    for i, task in enumerate(tasks, 1):
        start = task["start_date"] or "미정"
        end = task["end_date"] or "미정"
        components = ", ".join(task["components"]) if task["components"] else "미지정"

        # 시작일/종료일에 요일 정보 추가
        start_display = start
        end_display = end
        if start != "미정":
            try:
                start_dt = datetime.fromisoformat(start).date()
                start_display = f"{start} ({weekday_names[start_dt.weekday()]})"
            except ValueError:
                pass
        if end != "미정":
            try:
                end_dt = datetime.fromisoformat(end).date()
                end_display = f"{end} ({weekday_names[end_dt.weekday()]})"
            except ValueError:
                pass

        lines.append(f"{i}. {task['title']}")
        lines.append(f"   - 상태: {task['status']}")
        lines.append(f"   - 기간: {start_display} ~ {end_display}")
        lines.append(f"   - 구성요소: {components}")
        lines.append("")

    return "\n".join(lines)


def _evaluate_schedule_with_llm(assignee_name: str, tasks_text: str) -> dict:
    """LLM을 사용하여 일정 실현 가능성 평가"""
    client = OpenAI()

    system_prompt = """당신은 프로젝트 매니저로서 팀원의 일정 실현 가능성을 평가합니다.

중요: 영업일 기준으로 평가
- 토요일, 일요일, 공휴일은 근무일이 아님
- 일정 계산 시 영업일만 고려해야 함
- 제공되는 "향후 4주간 영업일 현황"을 참고하여 실제 작업 가능 일수를 계산
- 오늘 이전의 날짜에 대해서는 분석하지 않음 (과거 일정 겹침은 이미 지나간 일이므로 무시)

우리 팀의 작업 규칙:
- 각 작업의 마지막 1영업일 또는 전체 기간의 20%는 리뷰 기간임
- 리뷰 기간에는 풀타임으로 작업하지 않으므로, 리뷰 기간과 다른 작업이 겹치는 것은 완전히 정상임
- 작업이 순차적으로 배치되어 있으면 문제없음

평가 기준 (관대하게 평가, 영업일 기준):
- "불가능": 핵심 작업 기간(리뷰 기간 제외)이 3개 이상 동시에 겹칠 때만
- "주의필요": 핵심 작업 기간이 2개 동시에 겹치고, 그 기간이 영업일 기준 3일 이상일 때
- "가능": 그 외 모든 경우 (리뷰 기간 겹침, 순차 배치 등은 모두 정상)

응답 형식 (반드시 첫 줄에 판정 결과를 명시):
실현가능여부: [가능/불가능/주의필요]
분석: [문제가 되는 부분만 간결하게 1-2문장으로 설명. 정상인 부분(리뷰 기간 겹침, 순차 배치 등)은 언급하지 않음]
제안: [일정 조정이 필요한 경우 구체적인 제안, 필요 없으면 "없음"]

주의: Slack 포맷팅 규칙 - **bold**가 아니라 *bold* 사용. 마크다운 문법 사용 금지."""

    user_prompt = f"""담당자: {assignee_name}

작업 목록:
{tasks_text}

위 담당자의 일정 실현 가능성을 평가해주세요."""

    response = client.chat.completions.create(
        model="gpt-5.2",
        reasoning_effort="medium",  # 일정 분석을 위한 추론 강화
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    response_text = response.choices[0].message.content or ""

    first_line = response_text.split("\n")[0]
    is_feasible = True
    if "불가능" in first_line:
        is_feasible = False
    elif "주의필요" in first_line:
        is_feasible = None

    return {
        "is_feasible": is_feasible,
        "full_response": response_text,
    }


def _send_schedule_alert(
    slack_client: WebClient,
    channel_id: str,
    assignee_email: str,
    assignee_name: str,
    evaluation: dict,
    tasks: list[dict],
    email_to_user_id: dict,
    dry_run: bool = False,
):
    """일정 문제가 있는 담당자에게 Slack 알림 전송 (메인 메시지 + 스레드)"""
    slack_user_id = email_to_user_id.get(assignee_email)

    mention = (
        f"<@{slack_user_id}>" if slack_user_id else (assignee_name or assignee_email)
    )

    # 메인 메시지: 간단한 경고 + 멘션
    if evaluation["is_feasible"] is False:
        main_message = f"🚨 *일정 실현 불가능* - {mention}"
    else:
        main_message = f"⚠️ *일정 주의 필요* - {mention}"

    # 스레드용 세부 내용 구성
    # 작업 목록 (기간 정보 포함)
    task_summary = []
    for task in tasks:
        status_emoji = {"대기": "⏳", "진행": "🔄", "리뷰": "👀"}.get(
            task["status"], "📋"
        )
        start = task.get("start_date", "")[:10] if task.get("start_date") else "?"
        end = task.get("end_date", "")[:10] if task.get("end_date") else "?"
        task_summary.append(
            f"{status_emoji} <{task['url']}|{task['title']}> ({start}~{end})"
        )

    task_list_text = "\n".join(task_summary)

    # AI 응답 파싱
    full_response = evaluation["full_response"]
    analysis = ""
    suggestion = ""

    for line in full_response.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("분석:"):
            analysis = line_stripped[3:].strip()
        elif line_stripped.startswith("제안:"):
            suggestion = line_stripped[3:].strip()

    # 스레드 메시지 구성
    thread_parts = [
        f"*현재 작업 ({len(tasks)}개):*",
        task_list_text,
        "",
        f"*분석:* {analysis}" if analysis else "",
    ]

    # 제안이 있고 "없음"이 아닌 경우만 표시
    if suggestion and suggestion != "없음":
        thread_parts.append("")
        thread_parts.append(f"*제안:* {suggestion}")

    thread_parts.append("")
    thread_parts.append("일정 조정이 필요하면 로봇에게 요청해주세요.")

    thread_message = "\n".join(line for line in thread_parts if line is not None)

    if dry_run:
        print(f"[dry-run] 메인 메시지 (채널: {channel_id}):")
        print(main_message)
        print(f"[dry-run] 스레드 메시지:")
        print(thread_message)
        print("-" * 50)
    else:
        # 메인 메시지 전송 후 스레드로 세부 내용 전송
        response = slack_client.chat_postMessage(channel=channel_id, text=main_message)
        slack_client.chat_postMessage(
            channel=channel_id,
            text=thread_message,
            thread_ts=response["ts"],
        )


def run_schedule_feasibility_only(dry_run: bool = False):
    """일정 실현 가능성 평가만 실행 (테스트용)"""
    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    email_to_user_id = get_email_to_user_id(slack_client)

    alert_schedule_feasibility(
        notion,
        slack_client,
        MAIN_DATA_SOURCE_ID,
        MAIN_CHANNEL_ID,
        email_to_user_id,
        "e",
        dry_run=dry_run,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack 메시지를 보내지 않고 콘솔에 출력만",
    )
    parser.add_argument(
        "--schedule-only",
        action="store_true",
        help="일정 실현 가능성 평가만 실행",
    )
    args = parser.parse_args()

    if args.schedule_only:
        run_schedule_feasibility_only(dry_run=args.dry_run)
    else:
        main()
