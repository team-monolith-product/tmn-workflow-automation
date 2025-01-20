# daily_scrum.py
import os
import random
from datetime import datetime
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
    # 사용자 순서 랜덤 셔플
    random.shuffle(user_ids)

    sections = slack_client.canvases_sections_lookup(
        canvas_id=SLACK_CANVAS_ID,
        criteria={
            "contains_text": " "
        }
    )["sections"] + slack_client.canvases_sections_lookup(
        canvas_id=SLACK_CANVAS_ID,
        criteria={
            "contains_text": ":heart:"
        }
    )["sections"]



    # 캔버스 내용 지우기
    for section in sections:
        slack_client.canvases_edit(
            canvas_id=SLACK_CANVAS_ID,
            changes=[{'operation': 'delete', 'section_id': section['id']}]
        )

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

    # 캔버스 편집
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

if __name__ == "__main__":
    daily_scrum()
