# daily_scrum.py

import random
from datetime import datetime
from dotenv import load_dotenv

from apis.slack import get_slack_user_ids_in_channel, get_user_info, lookup_sections, edit_canvas

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
    # Slack 사용자 목록 가져오기
    user_ids = get_slack_user_ids_in_channel(SLACK_CHANNEL_ID)
    # 봇 사용자 제외
    user_id_to_user_info = {user_id: get_user_info(
        user_id) for user_id in user_ids}
    user_ids = [
        user_id for user_id in user_ids if not user_id_to_user_info[user_id].get('is_bot', False)
    ]
    # 사용자 순서 랜덤 셔플
    random.shuffle(user_ids)

    sections = lookup_sections(SLACK_CANVAS_ID)

    # 캔버스 내용 지우기
    for section in sections:
        edit_canvas(
            SLACK_CANVAS_ID, [{'operation': 'delete', 'section_id': section['id']}])

    # 캔버스 내용 생성
    today = datetime.now().strftime("%Y년 %m월 %d일")
    content = f"{today} 출석부\n"
    for user_id in user_ids:
        user_info = user_id_to_user_info[user_id]
        user_name = user_info.get('real_name', 'Unknown User')
        emoji = random.choice(emojis)
        content += f"- [ ] {user_name} {emoji}\n"

    # 캔버스 편집
    edit_canvas(SLACK_CANVAS_ID, [{
        'operation': 'insert_at_end',
        "document_content": {
            "type": "markdown",
            "markdown": content
        }
    }])


if __name__ == "__main__":
    daily_scrum()
