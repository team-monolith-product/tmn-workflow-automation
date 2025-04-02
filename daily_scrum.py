# daily_scrum.py
import argparse
import os
import random
from datetime import datetime
from typing import Dict, List
from dotenv import load_dotenv

import requests
from slack_sdk import WebClient

# 환경 변수 로드
load_dotenv()

# Slack 채널 ID
SLACK_CHANNEL_ID = 'C02JX95U7AP'

# print_conversation_info.py 를 통해 획득됨.
# 추가로 workflow automation app이 채널에 등록돼야함.
SLACK_CANVAS_ID = 'F05S8Q78CGZ'

# 슬랙 리마인더로 정해진 시간에 메세지를 보내며
# 이 파일을 실행하여 캔버스를 업데이트 합니다.
# /remind #--데일리-- 스크럼 시간입니다! 출석부를 작성해주세요 😆 @channel every weekday at 16:30pm
# /remind #--데일리-- 스크럼 시간입니다! 출석부를 작성해주세요 :laughing: @channel every weekday at 16:30pm

# 이모지 목록
emojis = ["😀", "😃", "😄", "😁", "😆", "😅", "😂", "🤣", "😊",
          "😇", ":party-blob:", ":sad_cat_thumbs_up:", "🥎", "💭",
          ":cat:", ":squirrel:", ":cubimal_chick:", ":face_with_spiral_eyes:",
          ":melting_face:", ":grin:", ":face_with_raised_eyebrow:",
          ":woman-bouncing-ball:", ":tada:"]


def daily_scrum():
    """
    --dry-run
      옵션이 주어지는 경우 실제 메시지를 전송하지 않고,
      대신 콘솔에 출력합니다.
    """
    # 명령행 인자 파싱
    parser = argparse.ArgumentParser(description="근무 시간 알림 스크립트")
    parser.add_argument('--dry-run', action='store_true',
                        help='메시지를 Slack에 전송하지 않고 콘솔에 출력합니다.')
    args = parser.parse_args()

    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # 1) 원티드스페이스에서 오늘자 WorkEvent(휴가/외근)를 받아옵니다.
    work_events = get_wantedspace_workevent().get('results', [])
    email_to_event = {}
    for event in work_events:
        email = event.get('email')
        event_name = event.get('event_name')
        if email and event_name:
            # 여러 건이 있을 수도 있으나, 보통은 하나만 쓰면 되므로 간단하게 처리
            email_to_event[email] = event_name

    # Slack 사용자 목록 가져오기
    user_ids = slack_client.conversations_members(
        channel=SLACK_CHANNEL_ID)["members"]
    
    # 봇 사용자 제외
    user_id_to_user_info = {
        user_id: slack_client.users_info(user=user_id)['user'] for user_id in user_ids
    }
    user_ids = [
        user_id for user_id in user_ids if not user_id_to_user_info[user_id].get('is_bot', False)
    ]

    # 최적의 스크럼 효율을 위해 참여자의 순서를 조작합니다.
    user_ids = shuffle(slack_client, user_ids)

    # 캔버스 내용 생성
    today = datetime.now().strftime("%Y년 %m월 %d일")
    content = f"{today} 출석부\n"
    for user_id in user_ids:
        user_info = user_id_to_user_info[user_id]
        user_name = user_info.get('real_name', 'Unknown User')
        emoji = random.choice(emojis)

        user_profile = user_info.get('profile', {})
        user_email = user_profile.get('email', "")

        # ex) '연차(오후)'
        event_reason = email_to_event.get(user_email, "")

        if event_reason:
            content += f"- [ ] {user_name} {emoji} - {event_reason}\n"
        else:
            content += f"- [ ] {user_name} {emoji}\n"

    if args.dry_run:
        # 실제 캔버스를 수정하지 않고 콘솔에 출력합니다.
        print(f"캔버스:\n{content}")
    else:
        # 캔버스 편집
        sections = slack_client.canvases_sections_lookup(
            canvas_id=SLACK_CANVAS_ID,
            criteria={
                "contains_text": " "
            }
        )["sections"]

        # 캔버스 내용 지우기
        for section in sections:
            slack_client.canvases_edit(
                canvas_id=SLACK_CANVAS_ID,
                changes=[{'operation': 'delete', 'section_id': section['id']}]
            )

        slack_client.canvases_edit(
            canvas_id=SLACK_CANVAS_ID,
            changes=[{
                'operation': 'insert_at_end',
                "document_content": {
                    "type": "markdown",
                    "markdown": content
                }
            }]
        )


def get_wantedspace_workevent():
    """
    Args:
        None

    Returns:
        {
            "next": None,
            "previous": None,
            "count": 3,
            "results": [
                {
                    "wk_start_date": "2025-01-03",
                    "wk_end_date": "2025-01-03",
                    "event_name": "연차(오후)",
                    "wk_counted_days": 0.5,
                    "wk_alter_days": 0.0,
                    "wk_comp_days": 0.0,
                    "status": "INFORMED",
                    "wk_location": "",
                    "wk_comment": "",
                    "username": "김바바",
                    "email": "kpapa@team-mono.com",
                    "eid": "",
                    "evt_start_time": "13:00:00",
                    "evt_end_time": "17:00:00",
                    "wk_event": "WNS_VACATION_PM",
                    "applied_days": 1
                },
                ...
            ]
        }
    """
    url = 'https://api.wantedspace.ai/tools/openapi/workevent/'
    query = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'key': os.environ.get('WANTEDSPACE_API_KEY')
    }
    headers = {
        'Authorization': os.environ.get('WANTEDSPACE_API_SECRET')
    }
    response = requests.get(url, params=query, headers=headers, timeout=10)
    return response.json()


def shuffle(
    slack_client: WebClient,
    user_ids: List[str],
) -> List[str]:
    """
    최적의 스크럼 효율을 위해 참여자의 순서를 조작합니다.
    - 기본적으로 무작위로 배치하여 매일 앞 사람의 발표에 집중하게 합니다.
    - 같은 팀 구성원들은 서로 가까이 배치하여 듣는 사람의 이해를 돕습니다.

    Args:
        user_ids (List[str]): 사용자 ID 목록

    Returns:
        List[str]: 무작위로 섞인 사용자 ID 목록
    """

    team_id_to_user_ids = get_team_id_to_user_ids(slack_client, user_ids)

    # 팀별로 사용자 ID를 무작위로 섞습니다.
    for team_id, uids in team_id_to_user_ids.items():
        team_id_to_user_ids[team_id] = random.sample(uids, len(uids))

    # 팀을 무작위로 섞습니다.
    team_ids = list(team_id_to_user_ids.keys())
    random.shuffle(team_ids)

    return [
        user_id for team in team_ids for user_id in team_id_to_user_ids[team]
    ]


def get_team_id_to_user_ids(
    slack_client: WebClient,
    user_ids: List[str],
) -> Dict[str | None, List[str]]:
    """
    Slack SDK를 사용하여 사용자 ID와 팀(사용자 그룹)을 매핑합니다.
    한 사용자가 여러 사용자 그룹에 속한다면,
    그 중 가장 작은 규모의 사용자 그룹을 선택합니다.

    Args:
        user_ids (List[str]): 사용자 ID 목록

    Returns:
        Dict: 사용자 ID와 팀 매핑
    """
    team_id_to_user_ids = {}
    usergroups_response = slack_client.usergroups_list()
    for group in usergroups_response["usergroups"]:
        team_id_to_user_ids[group["id"]] = slack_client.usergroups_users_list(usergroup=group["id"]).get("users", [])

    # 사용자 ID와 팀 매핑 (최소 규모 팀 )
    user_id_to_team_ids = {}
    for team_id, user_ids in team_id_to_user_ids.items():
        for user_id in user_ids:
            if user_id not in user_id_to_team_ids:
                user_id_to_team_ids[user_id] = []
            user_id_to_team_ids[user_id].append(team_id)

    # 최소 규모 팀을 선택
    user_id_to_smallest_team_id = {}
    for user_id, team_ids in user_id_to_team_ids.items():
        if len(team_ids) > 1:
            min_team_id = min(team_ids, key=lambda x: len(team_id_to_user_ids[x]))
            user_id_to_smallest_team_id[user_id] = min_team_id
        else:
            user_id_to_smallest_team_id[user_id] = team_ids[0]


    # 팀 ID가 없는 사용자 ID는 None으로 설정
    for user_id in user_ids:
        if user_id not in user_id_to_smallest_team_id:
            user_id_to_smallest_team_id[user_id] = None


    smallest_team_id_to_user_ids = {}
    for user_id, team_id in user_id_to_smallest_team_id.items():
        if team_id not in smallest_team_id_to_user_ids:
            smallest_team_id_to_user_ids[team_id] = []
        smallest_team_id_to_user_ids[team_id].append(user_id)

    return smallest_team_id_to_user_ids

if __name__ == "__main__":
    daily_scrum()
