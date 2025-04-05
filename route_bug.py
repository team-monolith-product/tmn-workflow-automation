"""
버그 신고 채널에 게시글이 올라오면,
그것을 다음과 같은 규칙을 바탕으로 담당자를 결정하여 멘션합니다.
- 출근을 한 사람을 더 높은 우선 순위로 할당
- 신고 내용과 관련한 팀 내에서 더 높은 우선 순위로 할당
- 최근 버그 담당 수가 적은 사람에게 할당

이 파일은 직접 실행되지 않고, 모듈로 import 되어 사용됩니다.
슬랙 봇(app.py)이 이 파일을 import 하여 사용하길 기대합니다.
"""

import json
import random
from typing import Literal
import datetime
from openai import OpenAI
from slack_sdk.web.async_client import AsyncWebClient

from api.wantedspace import get_worktime
from service.slack import get_email_to_user_id_async


async def route_bug(
    slack_client: AsyncWebClient,
    body: dict,
) -> None:
    """
    버그 신고 메시지를 받아 담당자를 결정하고 응답합니다.

    Args:
        body: Slack 이벤트 페이로드 딕셔너리
    """
    message_text = body.get("event", {}).get("text", "")
    channel_id = body.get("event", {}).get("channel")
    thread_ts = body.get("event", {}).get("ts")

    team, priority = extract_team_and_priority_from_report_text(message_text)
    working_emails = get_working_emails()
    email_to_user_id = await get_email_to_user_id_async(slack_client)
    team_to_emails = await get_team_to_emails(slack_client, email_to_user_id)
    email_to_bug_count = get_email_to_bug_count()
    assignee_email = select_assignee_email(
        team, priority, working_emails, team_to_emails, email_to_bug_count
    )
    update_bug_count(assignee_email)
    await send_slack_response(
        slack_client, channel_id, thread_ts, assignee_email, email_to_user_id
    )


def get_working_emails() -> list[str]:
    """워티드스페이스 API를 통해 현재 출근한 사용자 이메일 목록을 반환"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    worktime = get_worktime(date=today)

    working_emails = []
    if worktime and "results" in worktime:
        for user in worktime["results"]:
            if user["wk_end_time"] is None:
                working_emails.append(user["email"])

    return working_emails


def extract_team_and_priority_from_report_text(
    text,
) -> tuple[Literal["ie", "fe", "be"], Literal["보통", "높음", "긴급"]]:
    """버그 신고 메시지 내용을 분석하여 관련 팀/구성 요소 반환"""
    client = OpenAI()
    response = client.responses.create(
        model="gpt-4o",
        input=[
            {
                "role": "system",
                "content": """
                    당신은 버그 신고 내용을 분석하여 관련 팀과 우선순위를 결정하는 전문가입니다.
                    
                    팀 분류:
                    - fe: 프론트엔드 관련 버그 (UI, 사용자 상호작용, 브라우저 렌더링 등)
                    - be: 백엔드 관련 버그 (API, 데이터베이스, 서버 로직 등)
                    - ie: 인프라 관련 버그 (배포, 서버 환경, 네트워크, 성능 등)
                    
                    우선순위 분류:
                    - 신고 본문에 아래 분류가 직접 포함된다면 그 분류를 추출하세요. 그렇지 않다면 다음 기준을 사용하여 판단하세요.
                    - 긴급: 수 시간 내에 즉시 해결이 필요한 경우
                    - 높음: 며칠 내에 해결이 필요한 경우
                    - 보통: 버그가 과거에도 존재한 것으로 추정되며 해결이 시급하지 않은 경우
                    
                    사용자의 버그 신고 내용을 분석하여 JSON 형식으로 정확하게 응답하세요.
                    """,
            },
            {"role": "user", "content": text},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "team_and_priority",
                "schema": {
                    "type": "object",
                    "properties": {
                        "team": {
                            "type": "string",
                            "enum": ["ie", "fe", "be"],
                            "description": "버그와 관련된 팀 식별자",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["보통", "높음", "긴급"],
                            "description": "버그의 우선순위",
                        },
                    },
                    "required": ["team", "priority"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        },
    )

    team_and_priority = json.loads(response.output_text)
    return (
        team_and_priority["team"],
        team_and_priority["priority"],
    )


def get_email_to_bug_count() -> dict[str, int]:
    """최근 버그 할당 이력을 조회하여 사용자별 담당 건수 반환"""
    # 최근 일주일 간의 버그 할당 이력 조회
    # 간단하면서 영속적인 저장소 필요
    # TODO: 구현
    return {"lch@team-mono.com": 0}


def select_assignee_email(
    team: Literal["ie", "fe", "be"],
    priority: Literal["보통", "높음", "긴급"],
    working_emails: list[str],
    team_to_emails: dict[Literal["ie", "fe", "be"], list[str]],
    email_to_bug_count: dict[str, int],
) -> str:
    """주어진 조건에 따라 최적의 담당자 선택"""
    team_emails: list[str] = team_to_emails.get(team, [])
    all_emails: list[str] = [
        email for emails in team_to_emails.values() for email in emails
    ]
    working_team_emails: list[str] = [
        email for email in team_emails if email in working_emails
    ]
    all_working_emails: list[str] = [
        email for email in all_emails if email in working_emails
    ]

    candidate_emails: list[str] = []

    if priority == "긴급":
        # 1순위: 해당 팀에서 출근한 사람
        if working_team_emails:
            candidate_emails = working_team_emails
        # 2순위: 다른 팀이라도 출근한 사람
        elif all_working_emails:
            candidate_emails = all_working_emails
        # 3순위: 해당 팀 구성원
        else:
            candidate_emails = team_emails
    else:  # "보통" 또는 "높음"
        # 관련 팀 구성원 (출근 여부 상관없음)
        candidate_emails = team_emails

    # 후보가 없는 경우
    if not candidate_emails:
        # CTO
        return "lch@team-mono.com"

    # 각 후보의 버그 건수를 가져옴 (없으면 0으로 간주)
    email_bug_count_pairs: list[tuple[str, int]] = [
        (email, email_to_bug_count.get(email, 0)) for email in candidate_emails
    ]

    # 버그 건수가 가장 적은 값 찾기
    min_bug_count: int = min(count for _, count in email_bug_count_pairs)

    # 버그 건수가 가장 적은 후보들 선택
    min_bug_emails: list[str] = [
        email for email, count in email_bug_count_pairs if count == min_bug_count
    ]

    if not min_bug_emails:
        return "lch@team-mono.com"

    # 동일한 버그 건수를 가진 후보가 여러 명이면 무작위로 선택
    return random.choice(min_bug_emails)


async def send_slack_response(
    slack_client: AsyncWebClient,
    channel_id: str,
    thread_ts: str,
    assignee_email: str,
    email_to_user_id: dict[str, str],
) -> None:
    """
    슬랙에 담당자 지정 메시지를 전송합니다.

    Args:
        slack_client: 슬랙 클라이언트
        channel_id: 응답을 보낼 슬랙 채널 ID
        thread_ts: 응답할 스레드의 타임스탬프
        assignee_email: 할당된 담당자 이메일 (없을 경우 None)
    """
    await slack_client.chat_postMessage(
        channel=channel_id,
        text=f"버그 신고가 접수되었습니다. 담당자는 <@{email_to_user_id.get(assignee_email)}>입니다.",
        thread_ts=thread_ts,
    )


async def get_team_to_emails(
    slack_client: AsyncWebClient,
    email_to_user_id: dict[str, str],
) -> dict[Literal["ie", "fe", "be"], list[str]]:
    """특정 팀에 속한 구성원 목록 반환"""
    team_to_usergroup_id: dict[Literal["ie", "fe", "be"], str] = {
        "fe": "S07V4G2QJJY",
        "be": "S085DBK2TFD",
        "ie": "S08628PEEUQ",
    }
    team_to_user_ids: dict[Literal["ie", "fe", "be"], list[str]] = {
        k: (await slack_client.usergroups_users_list(usergroup=v))["users"]
        for k, v in team_to_usergroup_id.items()
    }  # type: ignore

    user_id_to_email = {v: k for k, v in email_to_user_id.items()}

    return {
        k: [
            user_id_to_email[user_id]
            for user_id in user_ids
            if user_id in user_id_to_email
        ]
        for k, user_ids in team_to_user_ids.items()
    }


def update_bug_count(assignee_email: str) -> None:
    """
    버그 할당 이력을 업데이트합니다.

    Args:
        assignee_email: 버그가 할당된 사용자의 이메일
    """
    pass


if __name__ == "__main__":
    # 테스트
    import os
    import dotenv
    import asyncio

    dotenv.load_dotenv()

    async def main():
        slack_client = AsyncWebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
        message_text = "버그 신고 내용입니다."
        team, priority = extract_team_and_priority_from_report_text(message_text)
        working_emails = get_working_emails()
        email_to_user_id = await get_email_to_user_id_async(slack_client)
        team_to_emails = await get_team_to_emails(slack_client, email_to_user_id)
        email_to_bug_count = get_email_to_bug_count()
        assignee_email = select_assignee_email(
            team, priority, working_emails, team_to_emails, email_to_bug_count
        )
        print(f"team, priority: {team}, {priority}")
        print(f"working_emails: {working_emails}")
        print(f"team_to_emails: {team_to_emails}")
        print(f"email_to_bug_count: {email_to_bug_count}")
        print(f"assignee_email: {assignee_email}")

    asyncio.run(main())
