"""
매일 16:00에 스크럼 메시지를 Slack 채널에 발송하는 스크립트
"""

import argparse
import os
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

from service.slack import get_email_to_user_id

# 환경 변수 로드
load_dotenv()

# 상수 정의
MAIN_DATA_SOURCE_ID = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"
SCRUM_CHANNEL_ID = "C09277NGUET"

# 팀별 Slack 그룹 ID
TEAM_GROUPS = {
    "기획": "S092KHHE0AF",
    "fe": "S07V4G2QJJY",
    "be": "S085DBK2TFD",
    "ie": "S08628PEEUQ",
}

USER_CHANGWHAN = "U02HT4EU4VD"


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(description="스크럼 메시지 발송 스크립트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다.",
    )
    args = parser.parse_args()

    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # 이메일 -> Slack User ID 매핑
    email_to_user_id = get_email_to_user_id(slack_client)

    if args.dry_run:
        print("=== DRY RUN MODE ===")
        print(f"채널: {SCRUM_CHANNEL_ID}\n")

    # 1. 안내 메시지 발송
    send_intro_message(slack_client, args.dry_run)

    # 2. 기획팀 스크럼
    send_planning_team_scrum(slack_client, args.dry_run)

    # 3. FE팀 스크럼
    send_team_scrum(notion, slack_client, email_to_user_id, "fe", "FE", args.dry_run)

    # 4. BE팀 스크럼
    send_team_scrum(notion, slack_client, email_to_user_id, "be", "BE", args.dry_run)

    # 5. IE팀 스크럼
    send_team_scrum(notion, slack_client, email_to_user_id, "ie", "IE", args.dry_run)

    # 6. 이창환 스크럼
    send_personal_scrum(slack_client, args.dry_run)

    if args.dry_run:
        print("\n=== DRY RUN COMPLETED ===")


def send_intro_message(slack_client: WebClient, dry_run: bool = False):
    """
    안내 메시지 발송

    Args:
        slack_client: Slack WebClient
        dry_run: True면 Slack에 메시지를 보내지 않고 콘솔에만 출력
    """
    intro_text = "오늘 스크럼을 시작합니다. 4:40까지 작성 부탁드립니다!"

    detail_text = """스크럼의 목적은 팀내의 진행상황을 확인하고, 장애를 파악하는 것입니다.
팀원이 스크럼 중 장애를 보고하면 각 마스터(김예원 엄은상 이창환)는
장애를 최소화 하기 위해 스크럼 후 다른 팀의 협조로 연계해주시면 되겠습니다.
다른 팀 스크럼이 궁금하시면 본인 팀의 스크럼이 끝나고 서면으로 남겨진 내용을 자유롭게 확인하시면 됩니다.
#a_스크럼_제품 #a_스크럼_고객 #a_스크럼_콘텐츠
다른 팀의 지원이 필요하시면 본인 팀 스크럼 때, 마스터를 통해 지원을 요청 주시면 됩니다.
(사실 스크럼이 아니더라도 업무 중 자유롭게 요청 주셔도 됩니다.)
---------------------------------------------------------------------------
오늘 한 일:
- XXX 운영 진행
내일 할 일:
- YYY 건에 대한 대응
오늘의 이슈:
- ZZZ 문제 발생, 해결 방안 모색 중
- AAA 이슈로 B팀과 협업 요청"""

    if dry_run:
        print(f"\n[안내 메시지]")
        print(intro_text)
        print(f"  └─ 스레드: {detail_text[:100]}...")
    else:
        # 메인 메시지 발송
        response = slack_client.chat_postMessage(
            channel=SCRUM_CHANNEL_ID,
            text=intro_text,
        )

        # 스레드에 상세 안내 추가
        thread_ts = response["ts"]
        slack_client.chat_postMessage(
            channel=SCRUM_CHANNEL_ID,
            thread_ts=thread_ts,
            text=detail_text,
        )


def send_planning_team_scrum(slack_client: WebClient, dry_run: bool = False):
    """
    기획팀 스크럼 메시지 발송 (Notion 조회 없이 멘션만)

    Args:
        slack_client: Slack WebClient
        dry_run: True면 Slack에 메시지를 보내지 않고 콘솔에만 출력
    """
    text = "기획팀 스크럼"

    if dry_run:
        print(f"\n[기획팀 스크럼]")
        print(text)
    else:
        slack_client.chat_postMessage(
            channel=SCRUM_CHANNEL_ID,
            text=text,
        )


def send_team_scrum(
    notion: NotionClient,
    slack_client: WebClient,
    email_to_user_id: dict[str, str],
    team_handle: str,
    team_name: str,
    dry_run: bool = False,
):
    """
    팀별 스크럼 메시지 발송 (Notion에서 진행 중인 태스크 조회)

    Args:
        notion: Notion Client
        slack_client: Slack WebClient
        email_to_user_id: 이메일 -> Slack User ID 매핑
        team_handle: 팀 핸들 (예: "fe", "be", "ie")
        team_name: 팀 이름 (예: "FE", "BE", "IE")
        dry_run: True면 Slack에 메시지를 보내지 않고 콘솔에만 출력
    """
    # 메인 메시지
    text = f"{team_name}팀 스크럼"

    if dry_run:
        print(f"\n[{team_name}팀 스크럼]")
        print(text)

    # 팀 멤버의 진행 중인 태스크 조회
    team_members = get_team_members(slack_client, team_handle)
    team_emails = [
        email
        for email, user_id in email_to_user_id.items()
        if user_id in team_members
    ]

    # Notion에서 진행 중인 태스크 조회
    in_progress_tasks = get_in_progress_tasks(notion, team_emails)

    # 이메일별로 태스크 그룹화
    email_to_tasks = {}
    for task in in_progress_tasks:
        email = task["email"]
        if email not in email_to_tasks:
            email_to_tasks[email] = []
        email_to_tasks[email].append(task)

    # 인원별로 메시지 포맷팅
    thread_messages = []
    for email, tasks in email_to_tasks.items():
        if not tasks:
            continue

        # 담당자 이름 (첫 번째 태스크에서 가져옴)
        assignee_name = tasks[0]["assignee_name"]

        # 인원별 메시지 생성
        person_message = f"{assignee_name}\n"
        for task in tasks:
            task_line = format_task_line(task)
            person_message += f"{task_line}\n"

        thread_messages.append(person_message.strip())

    if dry_run:
        # Dry run 모드에서는 콘솔에만 출력
        if thread_messages:
            for msg in thread_messages:
                print(f"  └─ {msg}")
        else:
            print(f"  └─ (진행 중인 태스크 없음)")
    else:
        # 실제 메시지 발송
        response = slack_client.chat_postMessage(
            channel=SCRUM_CHANNEL_ID,
            text=text,
        )
        thread_ts = response["ts"]

        # 인원별 메시지를 스레드에 발송
        for msg in thread_messages:
            slack_client.chat_postMessage(
                channel=SCRUM_CHANNEL_ID,
                thread_ts=thread_ts,
                text=msg,
            )


def send_personal_scrum(slack_client: WebClient, dry_run: bool = False):
    """
    이창환 개인 스크럼 메시지 발송

    Args:
        slack_client: Slack WebClient
        dry_run: True면 Slack에 메시지를 보내지 않고 콘솔에만 출력
    """
    text = "이창환 스크럼"

    if dry_run:
        print(f"\n[이창환 스크럼]")
        print(text)
    else:
        slack_client.chat_postMessage(
            channel=SCRUM_CHANNEL_ID,
            text=text,
        )


def get_team_members(slack_client: WebClient, team_handle: str) -> list[str]:
    """
    팀 핸들로 팀 멤버의 Slack User ID 목록 조회

    Args:
        slack_client: Slack WebClient
        team_handle: 팀 핸들 (예: "fe", "be", "ie")

    Returns:
        list[str]: Slack User ID 목록
    """
    # 사용자 그룹 ID 조회
    group_id = TEAM_GROUPS.get(team_handle)
    if not group_id:
        return []

    # 그룹 멤버 조회
    try:
        response = slack_client.usergroups_users_list(usergroup=group_id)
        return response.get("users", [])
    except Exception as e:
        print(f"Error fetching team members for {team_handle}: {e}")
        return []


def get_in_progress_tasks(
    notion: NotionClient, team_emails: list[str]
) -> list[dict[str, Any]]:
    """
    Notion에서 팀 멤버들의 진행 중인 태스크 조회

    Args:
        notion: Notion Client
        team_emails: 팀 멤버 이메일 목록

    Returns:
        list[dict]: 태스크 정보 목록
    """
    # 진행 상태의 태스크 조회
    results = notion.data_sources.query(
        **{
            "data_source_id": MAIN_DATA_SOURCE_ID,
            "filter": {"property": "상태", "status": {"equals": "진행"}},
        }
    )

    tasks = []
    for result in results.get("results", []):
        # 담당자 확인
        people = result["properties"]["담당자"].get("people", [])
        if not people:
            continue

        person = people[0].get("person")
        if not person:
            continue

        email = person.get("email")
        if email not in team_emails:
            continue

        # 태스크 정보 추출
        try:
            title_prop = result["properties"]["제목"]["title"]
            title = title_prop[0]["text"]["content"] if title_prop else "제목 없음"
        except (KeyError, IndexError):
            title = "제목 없음"

        # 타임라인 (마감일)
        timeline = result["properties"]["타임라인"].get("date")
        deadline = timeline["start"] if timeline else None

        # GitHub PR 연결 여부
        github_prs = result["properties"]["GitHub 풀 리퀘스트"]["relation"]
        has_pr = len(github_prs) > 0

        # 담당자 이름
        assignee_name = people[0].get("name", "")

        tasks.append(
            {
                "email": email,
                "title": title,
                "url": result["url"],
                "deadline": deadline,
                "has_pr": has_pr,
                "assignee_name": assignee_name,
            }
        )

    return tasks


def format_task_line(task: dict[str, Any]) -> str:
    """
    태스크 정보를 한 줄 형식으로 포맷팅 (인원별 그룹화에 사용)

    Args:
        task: 태스크 정보

    Returns:
        str: 포맷팅된 한 줄 메시지
    """
    # 마감일 계산
    deadline_text = ""
    warning_text = ""

    if task["deadline"]:
        deadline_date = datetime.fromisoformat(task["deadline"]).date()
        today = datetime.now().date()

        # 영업일 기준으로 마감까지 남은 일수 계산
        days_until_deadline = calculate_business_days(today, deadline_date)

        if days_until_deadline > 1:
            deadline_text = f" 마감 {days_until_deadline}일 전."
        elif days_until_deadline == 1:
            deadline_text = " 마감 1일 전."
        elif days_until_deadline == 0:
            deadline_text = " 마감 당일."
        else:
            deadline_text = f" 마감일 지남 ({abs(days_until_deadline)}일)."

        # PR 없음 경고 (마감 1일 전 또는 당일)
        if days_until_deadline <= 1 and not task["has_pr"]:
            warning_text = " PR이 없으므로 일정 조정이 필요합니다."

    # 메시지 포맷
    message = f"- <{task['url']}|{task['title']}>{deadline_text}{warning_text}"

    return message


def calculate_business_days(start_date, end_date) -> int:
    """
    시작일과 종료일 사이의 영업일 수 계산 (주말 제외)

    Args:
        start_date: 시작 날짜
        end_date: 종료 날짜

    Returns:
        int: 영업일 수
    """
    if start_date > end_date:
        return -calculate_business_days(end_date, start_date)

    business_days = 0
    current_date = start_date

    while current_date < end_date:
        # 월-금 (0-4)만 카운트
        if current_date.weekday() < 5:
            business_days += 1
        current_date += timedelta(days=1)

    return business_days


if __name__ == "__main__":
    main()
