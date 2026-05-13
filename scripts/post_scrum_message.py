"""
매일 16:00에 9시 스크럼 스레드에 진행 중인 Notion 태스크 요약을 답글로 발송하는 스크립트
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from datetime import datetime
from typing import Any

import sentry_sdk
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from slack_sdk import WebClient

from service.business_days import count_business_days
from service.config import (
    NotionDBConfig,
    ScrumSquadConfig,
    load_config,
)
from service.slack import find_thread_ts_by_text, get_email_to_user_id

# 환경 변수 로드
load_dotenv()


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(description="스크럼 태스크 요약 답글 스크립트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다.",
    )
    args = parser.parse_args()

    config = load_config()

    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"), notion_version="2025-09-03"
    )
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # 이메일 -> Slack User ID 매핑
    email_to_user_id = get_email_to_user_id(slack_client)

    if args.dry_run:
        print("=== DRY RUN MODE ===")

    scrum = config.scrum

    # 스쿼드별 태스크 요약 답글
    for squad in scrum.squads:
        try:
            reply_team_scrum_tasks(
                notion,
                slack_client,
                email_to_user_id,
                squad,
                scrum.pr_warning_excluded_members,
                args.dry_run,
            )
        except Exception as e:
            sentry_sdk.capture_exception(e)
            print(
                f"Error in reply_team_scrum_tasks for {squad.squad.display_name}: {e}"
            )

    if args.dry_run:
        print("\n=== DRY RUN COMPLETED ===")


def reply_team_scrum_tasks(
    notion: NotionClient,
    slack_client: WebClient,
    email_to_user_id: dict[str, str],
    squad: ScrumSquadConfig,
    pr_warning_excluded_members: list[str],
    dry_run: bool = False,
):
    """
    팀 스크럼 스레드에 진행 중인 Notion 태스크 요약을 답글로 발송

    Args:
        notion: Notion Client
        slack_client: Slack WebClient
        email_to_user_id: 이메일 -> Slack User ID 매핑
        squad: 스쿼드 설정
        pr_warning_excluded_members: PR 경고 제외 대상 Slack User ID 목록
        dry_run: True면 Slack에 메시지를 보내지 않고 콘솔에만 출력
    """
    # 팀 멤버의 진행 중인 태스크 조회
    team_members = get_team_members(slack_client, squad.squad.slack_usergroup_id)
    team_emails = [
        email for email, user_id in email_to_user_id.items() if user_id in team_members
    ]

    in_progress_tasks = get_in_progress_tasks(
        notion, team_emails, squad.squad.notion_db
    )

    # 이메일별로 태스크 그룹화
    email_to_tasks: dict[str, list[dict[str, Any]]] = {}
    for task in in_progress_tasks:
        email = task["email"]
        email_to_tasks.setdefault(email, []).append(task)

    # 인원별로 메시지 포맷팅
    thread_messages = []
    for email, tasks in email_to_tasks.items():
        if not tasks:
            continue

        assignee_name = tasks[0]["assignee_name"]
        user_id = email_to_user_id.get(email)
        is_excluded = user_id in pr_warning_excluded_members

        pr_warning_enabled = (
            squad.pr_warning
            and squad.squad.notion_db.properties.pr is not None
            and not is_excluded
        )

        person_message = f"{assignee_name}\n"
        for task in tasks:
            task_line = format_task_line(task, pr_warning_enabled)
            person_message += f"{task_line}\n"

        thread_messages.append(person_message.strip())

    if dry_run:
        print(f"\n[{squad.squad.display_name}] 채널: {squad.channel_id}")
        if thread_messages:
            for msg in thread_messages:
                print(f"  └─ {msg}")
        else:
            print(f"  └─ (진행 중인 태스크 없음)")
        return

    # 9시 스크럼 스레드 ts 찾기
    found = find_thread_ts_by_text(
        slack_client, squad.channel_id, [squad.squad.display_name]
    )
    thread_ts = found.get(squad.squad.display_name)
    if not thread_ts:
        print(
            f"[{squad.squad.display_name}] 스크럼 스레드를 찾지 못해 답글을 건너뜁니다."
        )
        return

    for msg in thread_messages:
        slack_client.chat_postMessage(
            channel=squad.channel_id,
            thread_ts=thread_ts,
            text=msg,
        )


def get_team_members(slack_client: WebClient, slack_usergroup_id: str) -> list[str]:
    """
    Slack 사용자 그룹 ID로 팀 멤버의 Slack User ID 목록 조회

    Args:
        slack_client: Slack WebClient
        slack_usergroup_id: Slack 사용자 그룹 ID

    Returns:
        list[str]: Slack User ID 목록
    """
    try:
        response = slack_client.usergroups_users_list(usergroup=slack_usergroup_id)
        return response.get("users", [])
    except Exception as e:
        print(f"Error fetching team members for {slack_usergroup_id}: {e}")
        return []


def get_in_progress_tasks(
    notion: NotionClient,
    team_emails: list[str],
    db_config: NotionDBConfig,
) -> list[dict[str, Any]]:
    """
    Notion에서 팀 멤버들의 진행 중인 태스크 조회

    Args:
        notion: Notion Client
        team_emails: 팀 멤버 이메일 목록
        db_config: Notion DB 설정

    Returns:
        list[dict]: 태스크 정보 목록
    """
    # 진행 중 상태의 태스크 조회
    status_filters = [
        {"property": db_config.properties.status, "status": {"equals": status}}
        for status in db_config.in_progress_statuses
    ]
    results = notion.data_sources.query(
        **{
            "data_source_id": db_config.data_source_id,
            "filter": {"or": status_filters},
        }
    )

    tasks = []
    for result in results.get("results", []):
        # 담당자 확인
        people = result["properties"][db_config.properties.assignee].get("people", [])
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
            title_prop = result["properties"][db_config.properties.title]["title"]
            title = title_prop[0]["text"]["content"] if title_prop else "제목 없음"
        except (KeyError, IndexError):
            title = "제목 없음"

        # 마감일
        date_value = result["properties"][db_config.properties.timeline].get("date")
        deadline = (
            date_value["end"]
            if date_value and date_value.get("end")
            else (date_value["start"] if date_value else None)
        )

        # GitHub PR 연결 여부
        has_pr = False
        if db_config.properties.pr:
            github_prs = result["properties"][db_config.properties.pr]["relation"]
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


def format_task_line(task: dict[str, Any], pr_warning_enabled: bool = True) -> str:
    """
    태스크 정보를 한 줄 형식으로 포맷팅 (인원별 그룹화에 사용)

    Args:
        task: 태스크 정보
        pr_warning_enabled: PR 경고 표시 여부

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
        days_until_deadline = count_business_days(today, deadline_date)

        if days_until_deadline > 1:
            deadline_text = f" 마감 {days_until_deadline}일 전."
        elif days_until_deadline == 1:
            deadline_text = " 마감 1일 전."
        elif days_until_deadline == 0:
            deadline_text = " 마감 당일."
        else:
            deadline_text = f" 마감일 지남 ({abs(days_until_deadline)}일)."

        # PR 없음 경고 (마감 1일 전 또는 당일, PR 경고 활성화된 경우)
        if days_until_deadline <= 1 and not task["has_pr"] and pr_warning_enabled:
            warning_text = " PR이 없으므로 일정 조정이 필요합니다."

    # 메시지 포맷
    message = f"- <{task['url']}|{task['title']}>{deadline_text}{warning_text}"

    return message


if __name__ == "__main__":
    main()
